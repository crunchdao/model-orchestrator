import os

from model_orchestrator.entities import ErrorType, CloudProviderErrorType, ModelRunnerErrorType, ModelRun, OrchestratorError, ModelRunsCluster
from model_orchestrator.entities.errors import OrchestratorErrorType, NEW_RELIC_ALERT_FLAG
from model_orchestrator.entities.exceptions import OrchestratorErrors
from model_orchestrator.entities.failure import Failure
from model_orchestrator.repositories import ModelRunRepository
from model_orchestrator.services.model_runs.run import _RunService
from model_orchestrator.state.models_state_subject import ModelsStateSubject
from model_orchestrator.utils.logging_utils import get_logger


class _ErrorHandlingService:
    def __init__(
        self,
        model_run_repository: ModelRunRepository,
        run_service: _RunService,
        state_subject: ModelsStateSubject,
        cluster: ModelRunsCluster,
        can_place_in_quarantine: bool,
    ):
        self.model_run_repository = model_run_repository
        self.run_service = run_service
        self.state_subject = state_subject
        self.cluster = cluster
        self.can_place_in_quarantine = can_place_in_quarantine

    def handle_error_from_exception(self, orchestrator_error: OrchestratorError):
        return self.handle_error(orchestrator_error.model_run,
                                 orchestrator_error.error_type,
                                 orchestrator_error.original_exception,
                                 orchestrator_error.original_exception_traceback,
                                 orchestrator_error.reason)

    def handle_error_from_exceptions(self, orchestrator_errors: OrchestratorErrors):
        for orchestrator_error in orchestrator_errors.errors:
            self.handle_error_from_exception(orchestrator_error)

    def handle_error(self, model_run: ModelRun, error_code: ErrorType, exception: Exception = None, traceback=None, reason: str = ""):
        if error_code == OrchestratorErrorType.STOP_BEFORE_CLEANUP:
            return self.finalize_model_cleanup(model_run)

        if not reason:
            reason = error_code.default_reason

        get_logger().info(f"Error encountered for model {model_run.model_id}: [Error Code: {error_code.value}] {reason}")
        if exception:
            get_logger().debug("Error details:", exc_info=exception)

        is_cloud_provider_error = isinstance(error_code, CloudProviderErrorType)
        place_to_quarantine = self.can_place_in_quarantine
        if error_code == OrchestratorErrorType.IN_QUARANTINE:
            place_to_quarantine = False
        if is_cloud_provider_error:
            place_to_quarantine = False
            get_logger().error(f"{NEW_RELIC_ALERT_FLAG} Something going wrong with cloud provider. ErrorType:[{ErrorType}], Reason:[{reason}]", exc_info=exception)
        elif error_code == ModelRunnerErrorType.CONNECTION_FAILED:
            place_to_quarantine = False
            get_logger().error(f"{NEW_RELIC_ALERT_FLAG} Connection to model runner (GRPC) failed. ErrorType:[{ErrorType}], Reason:[{reason}]", exc_info=exception)
        elif error_code == OrchestratorErrorType.STOP_UNEXPECTED:
            place_to_quarantine = False
            get_logger().error(f"{NEW_RELIC_ALERT_FLAG} Undesired stop of model {model_run.model_id}. ErrorType:[{ErrorType}], Reason:[{reason}]", exc_info=exception)

        model_run.record_failure(error_code, reason, exception, traceback)
        self.model_run_repository.save_model(model_run)

        if place_to_quarantine:
            model_run.place_in_quarantine()
            self.model_run_repository.save_model(model_run)

        # Stop the model if is running
        if model_run.is_run_active() and not is_cloud_provider_error:
            get_logger().info(f"Stopping model {model_run.model_id} due to error")
            self.run_service.stop_model(model_run)
        else:
            return self.finalize_model_cleanup(model_run)

    def finalize_model_cleanup(self, model_run: ModelRun):
        model_run.update_runner_status(model_run.runner_job_id, ModelRun.RunnerStatus.FAILED)
        self.model_run_repository.save_model(model_run)

        self.cluster.remove(model_run)
        # we can notify now if the stop of model not expected
        self.notify_failure(model_run)

    def notify_failure(self, model_run: ModelRun, failure: Failure = None):
        get_logger().debug(f"Notifying failure")

        self.state_subject.notify_failure(
            model_run=model_run,
            failure=failure if failure else model_run.failure,
        )
