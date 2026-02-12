import threading

from model_orchestrator.configuration.properties import AppConfig
from model_orchestrator.entities import ModelRunsCluster, OrchestratorError, ModelRun, CruncherOnchainInfo
from model_orchestrator.entities.crunch import Crunch
from model_orchestrator.entities.errors import OrchestratorErrorType, get_model_runner_error
from model_orchestrator.entities.exceptions import OrchestratorErrors

from model_orchestrator.repositories.augmented_model_info_repository import AugmentedModelInfoRepository
from model_orchestrator.repositories.crunch_repository import CrunchRepository
from model_orchestrator.repositories.model_run_repository import ModelRunRepository
from model_orchestrator.services import Builder
from model_orchestrator.services import Runner
from model_orchestrator.services.model_runs.build import _BuildService
from model_orchestrator.services.model_runs.error_handling import _ErrorHandlingService

from model_orchestrator.services.model_runs.run import _RunService
from model_orchestrator.state.models_state_subject import ModelsStateSubject
from model_orchestrator.utils.logging_utils import get_logger


class ModelRunsService:

    def __init__(
        self,
        model_runs_repository: ModelRunRepository,
        crunch_repository: CrunchRepository,
        builder: Builder,
        runner: Runner,
        state_subject: ModelsStateSubject,
        augmented_model_info_repository: AugmentedModelInfoRepository,
        app_config: AppConfig,
    ):
        self.failures_reported = []
        self.failures_reported_lock = threading.Lock()

        self.model_runs_repository = model_runs_repository
        self.crunch_repository = crunch_repository

        self.cluster = ModelRunsCluster(model_runs_repository.load_active())
        self.state_subject = state_subject

        self.run_service = _RunService(
            model_runs_repository,
            builder,
            runner,
            state_subject,
            self.cluster,
            augmented_model_info_repository
        )

        self.build_service = _BuildService(
            model_runs_repository,
            crunch_repository,
            builder,
            state_subject,
            self.cluster,
            lambda model, crunch: self.run_service.run_model(model, crunch)
        )

        self.error_handling = _ErrorHandlingService(
            model_runs_repository,
            self.run_service,
            state_subject,
            self.cluster,
            can_place_in_quarantine=app_config.can_place_in_quarantine,
        )

        self.lock = threading.Lock()

    def start_model(
        self,
        model_id: str,
        name: str,
        cruncher_id: str,  # todo deprecated
        code_submission_id: str,
        resource_id: str,
        hardware_type: ModelRun.HardwareType,
        crunch: Crunch,
        cruncher_wallet_pubkey: str = None,
        cruncher_hotkey: str = None
    ):
        crunch_id = crunch.id
        get_logger().info(f'Received start model request id:{model_id}, name:{name}, crunch ID: {crunch_id}')

        with self.lock:
            try:
                replaced_model = None
                running_models = self.cluster.get_running_models_by_model_id(model_id)
                if len(running_models) > 0:
                    if len(running_models) > 1:
                        get_logger().warning(f'More than one running model with the same ID, len:{len(running_models)}, id:{model_id}, name:{name}, crunch ID: {crunch_id}')

                    model = running_models[0]
                    get_logger().debug(f'Received start request for existing alive model ID:{model_id}, name:{name}, crunch ID: {crunch_id}')

                    if not model.has_changed(code_submission_id, resource_id, hardware_type):
                        get_logger().info(f'code/resource/hardware are not changed, we no start new model')
                        return
                    get_logger().debug(f'code/resource/hardware are changed, we start replacing of the model')

                    model.update_desired_status(ModelRun.DesiredStatus.REPLACING)
                    self.model_runs_repository.save_model(model=model)
                    replaced_model = model
                else:
                    get_logger().debug(f'No running model with the same ID, we start new model')

                self._start_new_model(
                    model_id=model_id,
                    name=name,
                    cruncher_id=cruncher_id,
                    code_submission_id=code_submission_id,
                    resource_id=resource_id,
                    hardware_type=hardware_type,
                    crunch=crunch,
                    replaced_model=replaced_model,
                    cruncher_wallet_pubkey=cruncher_wallet_pubkey,
                    cruncher_hotkey=cruncher_hotkey
                )
            except OrchestratorError as e:
                self.error_handling.handle_error_from_exception(e)
                return

    def _start_new_model(
        self,
        model_id: str,
        name: str,
        cruncher_id: str,  # will be deprecated
        code_submission_id: str,
        resource_id: str,
        hardware_type: ModelRun.HardwareType,
        crunch: Crunch,
        replaced_model: ModelRun = None,
        cruncher_wallet_pubkey: str = None,
        cruncher_hotkey: str = None
    ):
        model = ModelRun(
            None,
            model_id=model_id,
            name=name,
            crunch_id=crunch.id,
            cruncher_onchain_info=CruncherOnchainInfo(wallet_pubkey=cruncher_wallet_pubkey or cruncher_id, hotkey=cruncher_hotkey),
            code_submission_id=code_submission_id,
            resource_id=resource_id,
            hardware_type=hardware_type,
            desired_status=ModelRun.DesiredStatus.RUNNING
        )

        is_in_quarantine = self.model_runs_repository.is_model_in_quarantine(model.model_id, model.code_submission_id, model.resource_id, model.hardware_type)
        if is_in_quarantine:
            raise OrchestratorError(model, OrchestratorErrorType.IN_QUARANTINE)

        # checks if replacement model required build or its configs are same
        model.docker_image = replaced_model.docker_image if replaced_model and not model.is_build_required(replaced_model) else None

        self.model_runs_repository.save_model(model)
        self.cluster.add_model(model)

        # ok now I need to check if build is required, then I launch new building of docker image
        is_built, docker_image = self.build_service.is_built(model, crunch)
        if not is_built:
            get_logger().debug("Build is required")
            return self.build_service.build(model, crunch)
        else:
            get_logger().debug("Build is not required")
            model.set_docker_image(docker_image)
            self.model_runs_repository.save_model(model)

            # Check if model container is already running (CVM state survives orchestrator restarts)
            already_running = self.build_service.builder.check_already_running(model)

            if already_running:
                get_logger().info("Model %s already running in CVM — adopting (task=%s, port=%s)",
                                 model.model_id, already_running.get("task_id"), already_running.get("external_port"))
                task_id = already_running["task_id"]
                model.update_builder_status(task_id, ModelRun.BuilderStatus.SUCCESS)
                model.update_runner_status(task_id, ModelRun.RunnerStatus.INITIALIZING)
                model.set_runner_info({"spawntee_task_id": task_id})
                self.model_runs_repository.save_model(model)
                self.state_subject.notify_runner_state_changed(model, None, model.runner_status)
                return model
            else:
                return self.run_service.run_model(model, crunch)

    def stop_model(self, model_id: str):
        get_logger().info(f'Received stop model request id:{model_id}')
        with self.lock:
            running_models = self.cluster.get_active_models_by_model_id(model_id)
            if len(running_models) > 0:
                if len(running_models) > 1:
                    get_logger().warning(f'More than one running model with the same ID, len:{len(running_models)}, id:{model_id}')

                model = running_models[0]
                get_logger().debug(f'Received stop request for existing alive model ID:{model_id}, name:{model.name}, crunch ID: {model.crunch_id}')

                try:
                    self.run_service.stop_model(model)
                except OrchestratorError as e:
                    self.error_handling.handle_error_from_exception(e)
                    return

            else:
                get_logger().debug(f'No running model with the same ID, maybe is already stopped or currently stopping')

    def updates_states(self):

        services = [
            self.run_service.update_models_info,
            self.run_service.update_runner_states,
            self.build_service.update_builder_states,
        ]

        failures_reported = []
        with self.failures_reported_lock:
            failures_reported = self.failures_reported.copy()
            self.failures_reported.clear()

        with self.lock:
            # treat here the stopping and reporting or failure to the user
            for failure_code, model_id, ip in failures_reported:
                for model_item in self.cluster.get_models_by_model_id(model_id):
                    if model_item.ip == ip:
                        model_run = model_item
                        break
                else:
                    get_logger().warning(f"Report failure:{failure_code} for the model_id:{model_id} and ip:{ip} not found in cluster")
                    return

                self.error_handling.handle_error_from_exception(OrchestratorError(model_run, get_model_runner_error(failure_code)))

            # Here doing update of states from AWS
            for service in services:
                try:
                    service()
                except OrchestratorErrors as e:
                    self.error_handling.handle_error_from_exceptions(e)

    def get_running_models(self):
        with self.lock:
            running_models = self.cluster.get_running_models().copy()
        return running_models

    def get_running_models_by_crunch_id(self, crunch_id: str):
        with self.lock:
            return self.cluster.get_running_models_by_crunch_id(crunch_id).copy()

    def is_model_in_crunch(self, model_id: str, crunch_id: str) -> bool:
        with self.lock:
            return self.cluster.is_model_in_crunch(model_id, crunch_id)

    def get_all_models(self):
        with self.lock:
            models = self.model_runs_repository.load_all_latest()
            self.run_service.update_models_info(models)
        return models

    # error reported from coordinator
    def report_model_runner_failure(self, failure_code, model_id, ip):
        with self.failures_reported_lock:
            self.failures_reported.append((failure_code, model_id, ip))
