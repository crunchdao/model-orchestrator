import traceback
from typing import Callable

from model_orchestrator.entities import ModelRunsCluster, ModelRun, Crunch, OrchestratorError
from model_orchestrator.entities.errors import NEW_RELIC_ALERT_FLAG
from model_orchestrator.entities.exceptions import OrchestratorErrors
from model_orchestrator.repositories import ModelRunRepository, CrunchRepository
from model_orchestrator.services import Builder
from model_orchestrator.state.models_state_subject import ModelsStateSubject
from model_orchestrator.utils.logging_utils import get_logger
from model_orchestrator.entities import OrchestratorErrorType, CloudProviderErrorType, ModelRunnerErrorType


# Never use this class outside ModelRusService
class _BuildService:
    def __init__(self,
                 model_runs_repository: ModelRunRepository,
                 crunch_repository: CrunchRepository,
                 builder: Builder,
                 state_subject: ModelsStateSubject,
                 cluster: ModelRunsCluster,
                 on_model_built: Callable[[ModelRun, Crunch], None],
                 ):

        self.model_runs_repository = model_runs_repository
        self.crunch_repository = crunch_repository
        self.builder = builder
        self.state_subject = state_subject
        self.cluster = cluster

        if on_model_built is None:
            raise ValueError("on_model_built callback is required")
        self.on_model_built = on_model_built

    def build(self, model: ModelRun, crunch: Crunch):
        model.update_builder_status(None, ModelRun.BuilderStatus.BUILDING)
        self.model_runs_repository.save_model(model)

        try:
            job_id, docker_image, log_arn = self.builder.build(model, crunch)
        except Exception as e:
            raise OrchestratorError(model, CloudProviderErrorType.BUILD_EXCEPTION, original_exception=e, original_exception_traceback=traceback.format_exc())

        get_logger().debug(f"Building launch with job id: {job_id}, docker image: {docker_image}, logs arn: {log_arn}")
        model.set_docker_image(docker_image)
        model.update_builder_status(job_id, ModelRun.BuilderStatus.BUILDING)
        model.set_builder_logs_arn(log_arn)
        self.model_runs_repository.save_model(model)

        self.state_subject.notify_build_state_changed(model, None, model.builder_status)

        return model

    def is_built(self, model: ModelRun, crunch: Crunch):
        try:
            return self.builder.is_built(model, crunch)
        except Exception as e:
            get_logger().error(f"Error while checking if model is built", exc_info=e)
            return False, None

    def update_builder_states(self):
        models = self.cluster.get_models_in_build_phase()
        try:
            models_states = self.builder.load_statuses(models)
        except Exception as e:
            get_logger().error(f"{NEW_RELIC_ALERT_FLAG} Something going wrong with cloud provider. Impossible to load models build status", exc_info=e)
            return

        get_logger().trace(f'_update_builder_states expected: {len(models)} vs actual: {len(models_states)}')

        errors: list[OrchestratorError] = []

        for model, updated_status in models_states.items():

            # save changed state
            previous_status = model.builder_status
            if updated_status != previous_status:
                get_logger().debug(f'Builder Model {model.model_id} state changed from {model.builder_status} to {updated_status}')
                model.update_builder_status(model.builder_job_id, updated_status)
                self.model_runs_repository.save_model(model)

                self.state_subject.notify_build_state_changed(model, previous_status, updated_status)

                if updated_status == ModelRun.BuilderStatus.FAILED:
                    errors.append(OrchestratorError(model, OrchestratorErrorType.BUILD_FAILED, original_exception=None))

            if updated_status == ModelRun.BuilderStatus.SUCCESS:
                if model.desired_status == ModelRun.DesiredStatus.STOPPED:
                    get_logger().info(f'Model {model.model_id} is requested to stopped and is built, we ignore the run request and mark it as stopped')
                    model.update_desired_status(ModelRun.DesiredStatus.STOPPED)
                    self.model_runs_repository.save_model(model)
                else:
                    get_logger().debug(f'Model {model.model_id} is built, we can run it')
                    crunch_id = self.crunch_repository.get(model.crunch_id)
                    self.on_model_built(model, crunch_id)

        if errors:
            raise OrchestratorErrors(errors)
