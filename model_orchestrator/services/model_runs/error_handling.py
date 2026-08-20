import os
from datetime import datetime, timedelta, timezone

from model_orchestrator.entities import ErrorType, CloudProviderErrorType, ModelRunnerErrorType, ModelRun, OrchestratorError, ModelRunsCluster
from model_orchestrator.entities.errors import OrchestratorErrorType
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
        connection_failure_grace_minutes: int = 5,
        connection_failure_quiet_gap_minutes: int = 2,
    ):
        self.model_run_repository = model_run_repository
        self.run_service = run_service
        self.state_subject = state_subject
        self.cluster = cluster
        self.can_place_in_quarantine = can_place_in_quarantine
        self.connection_failure_grace_period = timedelta(minutes=connection_failure_grace_minutes)
        self.connection_failure_quiet_gap = timedelta(minutes=connection_failure_quiet_gap_minutes)

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
            get_logger().error(f"Cloud provider error. ErrorType:[{error_code}], Reason:[{reason}]", exc_info=exception)
        elif error_code == ModelRunnerErrorType.CONNECTION_FAILED:
            get_logger().error(f"Connection to model runner (GRPC) failed. ErrorType:[{error_code}], Reason:[{reason}]", exc_info=exception)
            # The model runs as an ECS service with restart-on-failure already handled by AWS
            # (deployment circuit breaker + AwsEcsModelRunner._check_excessive_restarts). A lone
            # report, or a burst of them, is usually that self-healing in progress (e.g. AWS host
            # maintenance swapping the underlying instance) - stopping here would kill the service
            # mid-recovery. But a steady stream of reports with no quiet gap for longer than the
            # grace period is not infra self-healing anymore (that only takes a couple minutes) -
            # it means the model is unreachable for real (e.g. cluster/security-group
            # misconfiguration), so escalate to the normal stop/finalize path below.
            if not self._is_persistent_connection_failure(model_run):
                self._record_failure(model_run, error_code, reason, exception, traceback)
                return
            get_logger().error(f"Model {model_run.model_id} has been unreachable (GRPC) for over {self.connection_failure_grace_period}, treating as a real failure")
            place_to_quarantine = False
        elif error_code == OrchestratorErrorType.STOP_UNEXPECTED:
            place_to_quarantine = False
            get_logger().error(f"Undesired stop of model {model_run.model_id}. ErrorType:[{error_code}], Reason:[{reason}]", exc_info=exception)

        self._record_failure(model_run, error_code, reason, exception, traceback)

        if place_to_quarantine:
            model_run.place_in_quarantine()
            self.model_run_repository.save_model(model_run)

        # Stop the model if is running
        if model_run.is_run_active() and not is_cloud_provider_error:
            get_logger().info(f"Stopping model {model_run.model_id} due to error")
            self.run_service.stop_model(model_run)
        else:
            return self.finalize_model_cleanup(model_run)

    def _record_failure(self, model_run: ModelRun, error_code: ErrorType, reason: str, exception: Exception, traceback):
        try:
            infra_reason = self.run_service.model_runner.describe_stop_reason(model_run)
        except Exception:
            get_logger().debug(f"Failed to fetch infra stop reason for model {model_run.model_id}", exc_info=True)
            infra_reason = None

        model_run.record_failure(error_code, reason, exception, traceback, infra_reason=infra_reason)
        self.model_run_repository.save_model(model_run)

    def _is_persistent_connection_failure(self, model_run: ModelRun) -> bool:
        """
        Tracks CONNECTION_FAILED reports in model_run.runner_info to tell apart a transient
        infra blip (self-heals) from a genuinely broken connection (never recovers).

        A streak starts on the first report and extends as long as reports keep arriving
        within `connection_failure_quiet_gap` of each other. If a streak's total duration
        passes `connection_failure_grace_period`, the failure is treated as persistent/real.
        A gap longer than the quiet gap resets the streak, since that means it did recover.
        """
        now = datetime.now(timezone.utc)
        runner_info = model_run.runner_info if model_run.runner_info is not None else {}

        streak_started_at = self._parse_datetime(runner_info.get('connection_failure_streak_started_at'))
        last_failure_at = self._parse_datetime(runner_info.get('connection_failure_last_at'))

        if streak_started_at is None or last_failure_at is None or (now - last_failure_at) > self.connection_failure_quiet_gap:
            streak_started_at = now

        is_persistent = (now - streak_started_at) >= self.connection_failure_grace_period

        if is_persistent:
            runner_info.pop('connection_failure_streak_started_at', None)
            runner_info.pop('connection_failure_last_at', None)
        else:
            runner_info['connection_failure_streak_started_at'] = streak_started_at.isoformat()
            runner_info['connection_failure_last_at'] = now.isoformat()

        model_run.set_runner_info(runner_info)
        return is_persistent

    @staticmethod
    def _parse_datetime(value: str | None) -> datetime | None:
        if not value:
            return None
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed

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
