import traceback

from click.core import augment_usage_errors
from exceptiongroup import catch

from model_orchestrator.entities import ModelRun, Crunch, ModelRunsCluster, OrchestratorError, CloudProviderErrorType, OrchestratorErrorType
from model_orchestrator.entities.errors import NEW_RELIC_ALERT_FLAG
from model_orchestrator.entities.exceptions import OrchestratorErrors
from model_orchestrator.repositories.augmented_model_info_repository import AugmentedModelInfoRepository
from model_orchestrator.repositories.model_run_repository import ModelRunRepository
from model_orchestrator.services import Runner, Builder
from model_orchestrator.state.models_state_subject import ModelsStateSubject
from model_orchestrator.utils.logging_utils import get_logger


# Never use this class outside ModelRusService
class _RunService:

    def __init__(self, model_runs_repository: ModelRunRepository,
                 builder: Builder,
                 runner: Runner,
                 state_subject: ModelsStateSubject,
                 cluster: ModelRunsCluster,
                 augmented_model_info_repository: AugmentedModelInfoRepository):

        self.model_runs_repository = model_runs_repository
        self.model_builder = builder
        self.model_runner = runner
        self.state_subject = state_subject
        self.cluster = cluster
        self.augmented_model_info_repository = augmented_model_info_repository

        self.latest_augmented_models_info = {}

    def run_model(self, model: ModelRun, crunch: Crunch):
        model.update_runner_status(None, ModelRun.RunnerStatus.INITIALIZING)
        self.model_runs_repository.save_model(model)

        get_logger().debug(f"Run model id: {model.model_id}")
        try:
            job_id, logs_arn, runner_info = self.model_runner.run(model, crunch)
        except Exception as e:
            raise OrchestratorError(model, CloudProviderErrorType.RUN_EXCEPTION, e, traceback.format_exc())

        get_logger().debug(f"Running launch with job id: {job_id}, logs arn: {logs_arn}, runner info: {runner_info}")
        model.update_runner_status(job_id, ModelRun.RunnerStatus.INITIALIZING)
        model.set_runner_info(runner_info)
        model.set_runner_logs_arn(logs_arn)
        self.model_runs_repository.save_model(model)

        self.state_subject.notify_runner_state_changed(model, None, model.runner_status)
        return model

    def stop_model(self, model: ModelRun):
        model.update_desired_status(ModelRun.DesiredStatus.STOPPED)
        self.model_runs_repository.save_model(model)

        # Notify early to prevent any undesired communication with the model during the stop process
        self.state_subject.notify_runner_state_changed(model, model.runner_status, ModelRun.RunnerStatus.STOPPED)

        get_logger().debug(f"Stopping model id: {model.model_id}")
        if model.in_run_phase():
            try:
                self.model_runner.stop(model)
            except Exception as e:
                raise OrchestratorError(model, CloudProviderErrorType.RUN_EXCEPTION, e, traceback.format_exc())

        else:
            get_logger().debug(f"Model is in build phase, we do not need to stop it")
            self.cluster.models.remove(model)

    def update_models_info(self, models = None):
        models = self.cluster.get_models_in_run_phase() if models is None else models
        try:
            self.latest_augmented_models_info = self.augmented_model_info_repository.loads([model.model_id for model in models])
        except Exception as e:
            get_logger().error(f"{NEW_RELIC_ALERT_FLAG} Something going wrong with API provider. Impossible to load models augmented info", exc_info=e)
            get_logger().debug(f"Failed to load models augmented info, we will use latest known info instead")
            return

        for model in models:
            model_info = self.latest_augmented_models_info.get(model.model_id)
            if model_info is None:
                continue

            prev_model_info = model.augmented_info
            model.augmented_info = model_info
            if model.is_running() and prev_model_info != model_info:
                self.state_subject.notify_runner_state_changed(model, model.runner_status, model.runner_status)

    def update_runner_states(self):
        # first pass of model runner
        models = self.cluster.get_models_in_run_phase()
        models_in_run_phase = []

        errors: list[OrchestratorError] = []

        # Here we handle unexpected states
        for model in models:
            if model.runner_status == ModelRun.RunnerStatus.INITIALIZING and model.runner_job_id is None:
                get_logger().warning(f'Model {model.model_id} is in initializing state, but no jobId is present!! something was wong')
                # TODO: Compare known models with those running on AWS to identify and track any unmonitored instances ?
                errors.append(OrchestratorError(model, OrchestratorErrorType.UNKNOWN))
                continue

            models_in_run_phase.append(model)

        models_states = None
        try:
            models_states = self.model_runner.load_statuses(models_in_run_phase)
        except Exception as e:
            get_logger().error(f"{NEW_RELIC_ALERT_FLAG} Something going wrong with cloud provider. Impossible to load models run status", exc_info=e)

        if models_states:
            get_logger().trace(f'update_runner_states expected: {len(models)} vs actual: {len(models_states)}')
            for model, (updated_status, ip, port) in models_states.items():

                # save changed state
                previous_status = model.runner_status
                if updated_status != previous_status:
                    get_logger().debug(f'Model {model.model_id} state changed from {model.runner_status} to {updated_status}')

                    model.update_runner_status(model.runner_job_id, updated_status, ip=ip, port=port)
                    self.model_runs_repository.save_model(model)
                    self.state_subject.notify_runner_state_changed(model, previous_status, updated_status)

                    # case replacement of the model
                    if updated_status == ModelRun.RunnerStatus.RUNNING:
                        get_logger().info(f'Model {model.model_id} is RUNNING')
                        replacing_model = self.cluster.get_models_by_model_id_and_desired_status(model.model_id, [ModelRun.DesiredStatus.REPLACING])
                        if len(replacing_model) > 0:
                            get_logger().info(f'Runner Model {model.model_id} is replacing model {replacing_model[0].model_id} and is now running, we can stop replaced model')
                            try:
                                self.stop_model(replacing_model[0])
                            except OrchestratorError as e:
                                errors.append(e)

                    elif updated_status == ModelRun.RunnerStatus.FAILED:
                        errors.append(OrchestratorError(model, OrchestratorErrorType.RUN_FAILED))

                    elif updated_status == ModelRun.RunnerStatus.STOPPED:
                        get_logger().info(f'Model {model.model_id} is STOPPED')

                        if model.has_failed():
                            errors.append(OrchestratorError(model, OrchestratorErrorType.STOP_BEFORE_CLEANUP))

                        elif model.desired_status != ModelRun.DesiredStatus.STOPPED:
                            # undesired stop
                            errors.append(OrchestratorError(model, OrchestratorErrorType.STOP_UNEXPECTED))
                        else:
                            self.cluster.remove(model)

        if errors:
            raise OrchestratorErrors(errors)
