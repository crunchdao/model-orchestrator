import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from model_orchestrator.entities import ModelRun, ModelRunnerErrorType, CruncherOnchainInfo
from model_orchestrator.services.model_runs.error_handling import _ErrorHandlingService


class TestErrorHandlingService(unittest.TestCase):
    def setUp(self):
        self.model_run_repository = MagicMock()
        self.run_service = MagicMock()
        self.state_subject = MagicMock()
        self.cluster = MagicMock()

        self.error_handling = _ErrorHandlingService(
            model_run_repository=self.model_run_repository,
            run_service=self.run_service,
            state_subject=self.state_subject,
            cluster=self.cluster,
            can_place_in_quarantine=True,
        )

        self.model_run = ModelRun(
            id=None,
            model_id="mock_model_id",
            name="mock_name",
            crunch_id="mock_crunch_id",
            cruncher_onchain_info=CruncherOnchainInfo(wallet_pubkey="pubkey", hotkey="hotkey"),
            code_submission_id="mock_code_submission_id",
            resource_id="mock_resource_id",
            hardware_type=ModelRun.HardwareType.GPU,
            desired_status=ModelRun.DesiredStatus.RUNNING,
        )
        self.model_run.update_runner_status("job_id", ModelRun.RunnerStatus.RUNNING, ip="127.0.0.1", port=50051)

    def test_connection_failed_does_not_stop_the_model(self):
        """
        A CONNECTION_FAILED report is a racy signal from the in-container coordinator - the ECS
        service is already self-healing (RECOVERING/restart tracking). We must not stop/finalize
        the model here, only record the failure and let the ECS polling loop decide the outcome.
        """
        self.error_handling.handle_error(self.model_run, ModelRunnerErrorType.CONNECTION_FAILED)

        self.run_service.stop_model.assert_not_called()
        self.cluster.remove.assert_not_called()
        self.state_subject.notify_failure.assert_not_called()

        self.assertIsNotNone(self.model_run.failure)
        self.model_run_repository.save_model.assert_called_with(self.model_run)

        # runner status untouched - left for update_runner_states() to reconcile from real ECS state
        self.assertEqual(self.model_run.runner_status, ModelRun.RunnerStatus.RUNNING)

    def test_connection_failed_burst_within_grace_period_is_tolerated(self):
        """
        Repeated CONNECTION_FAILED reports (e.g. every failed predict call during an AWS host
        swap) should still be tolerated as long as the whole streak stays under the grace period.
        """
        for _ in range(5):
            self.error_handling.handle_error(self.model_run, ModelRunnerErrorType.CONNECTION_FAILED)

        self.run_service.stop_model.assert_not_called()
        self.cluster.remove.assert_not_called()

    def test_connection_failed_persistent_streak_escalates_to_stop(self):
        """
        A streak that has been going on longer than the grace period (e.g. a real cluster
        misconfiguration that never recovers) must escalate to the normal stop/finalize path,
        not be swallowed forever.
        """
        old_start = datetime.now(timezone.utc) - timedelta(minutes=10)
        recent_report = datetime.now(timezone.utc) - timedelta(seconds=30)
        self.model_run.runner_info = {
            'connection_failure_streak_started_at': old_start.isoformat(),
            'connection_failure_last_at': recent_report.isoformat(),
        }

        self.error_handling.handle_error(self.model_run, ModelRunnerErrorType.CONNECTION_FAILED)

        self.run_service.stop_model.assert_called_once_with(self.model_run)

    def test_infra_stop_reason_is_captured_on_tolerated_failure(self):
        self.run_service.model_runner.describe_stop_reason.return_value = (
            "stopCode=ServiceSchedulerInitiated exitCode=137 reason=ECS is performing maintenance..."
        )

        self.error_handling.handle_error(self.model_run, ModelRunnerErrorType.CONNECTION_FAILED)

        self.assertEqual(
            self.model_run.failure.infra_reason,
            "stopCode=ServiceSchedulerInitiated exitCode=137 reason=ECS is performing maintenance...",
        )

    def test_infra_stop_reason_is_captured_on_escalated_failure(self):
        old_start = datetime.now(timezone.utc) - timedelta(minutes=10)
        recent_report = datetime.now(timezone.utc) - timedelta(seconds=30)
        self.model_run.runner_info = {
            'connection_failure_streak_started_at': old_start.isoformat(),
            'connection_failure_last_at': recent_report.isoformat(),
        }
        self.run_service.model_runner.describe_stop_reason.return_value = "stopCode=UserInitiated exitCode=1 reason=some real bug"

        self.error_handling.handle_error(self.model_run, ModelRunnerErrorType.CONNECTION_FAILED)

        self.assertEqual(self.model_run.failure.infra_reason, "stopCode=UserInitiated exitCode=1 reason=some real bug")

    def test_infra_stop_reason_lookup_failure_does_not_block_recording(self):
        """
        describe_stop_reason() is a live AWS call sitting in the error-handling path - if it
        blows up, failure recording must still proceed (just without infra_reason).
        """
        self.run_service.model_runner.describe_stop_reason.side_effect = Exception("boom")

        self.error_handling.handle_error(self.model_run, ModelRunnerErrorType.CONNECTION_FAILED)

        self.assertIsNotNone(self.model_run.failure)
        self.assertIsNone(self.model_run.failure.infra_reason)
        self.model_run_repository.save_model.assert_called_with(self.model_run)


if __name__ == "__main__":
    unittest.main()
