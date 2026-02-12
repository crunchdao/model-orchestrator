import asyncio
import unittest
from unittest.mock import Mock, MagicMock, ANY

from model_orchestrator.entities import ModelRun, OrchestratorErrorType, ModelRunnerErrorType, CloudProviderErrorType, ModelInfo, CruncherOnchainInfo
from model_orchestrator.services.builder import Builder
from model_orchestrator.services.model_runs import ModelRunsService


class TestModelRunsService(unittest.TestCase):
    def setUp(self):
        self.builder = MagicMock(spec=Builder)
        self.builder.check_already_running.return_value = None
        self.runner = MagicMock()
        self.state_subject_mock = MagicMock()

        self.model_runs_repository_mock = MagicMock()
        self.crunch_repository_mock = MagicMock()
        self.model_runs_repository_mock.load_active = Mock(return_value=[])
        self.augmented_model_run_repository_mock = MagicMock()
        self.augmented_model_run_repository_mock.loads = Mock(return_value={})

        self.model_runs_service = ModelRunsService(
            builder=self.builder,
            runner=self.runner,
            state_subject=self.state_subject_mock,
            model_runs_repository=self.model_runs_repository_mock,
            crunch_repository=self.crunch_repository_mock,
            augmented_model_info_repository=self.augmented_model_run_repository_mock,
            app_config=MagicMock(can_place_in_quarantine=True)
        )

        # Mock the lock for simplicity
        self.model_runs_service.lock = MagicMock()

    def test_model_run_scenario(self):
        """
        This test follows a full lifecycle scenario of a "model run":
        Start model, Build progress, Run the model then Stop the model
        """

        self.builder.is_built = Mock(return_value=(False, None))
        self.builder.build = Mock(return_value=("job_id", "docker_image", "log_arn"))

        self.builder.load_statuses = Mock(side_effect=lambda models: {model: ModelRun.BuilderStatus.SUCCESS for model in models})

        # Runner mock: simulate run states
        self.runner.run = Mock(return_value=("job_id", "log_arn", '{"runner_info":"value"}'))

        def status_generator():
            yield lambda models: {}
            yield lambda models: {model: (ModelRun.RunnerStatus.RUNNING, '127.0.0.1', 50051) for model in models}
            yield lambda models: {model: (ModelRun.RunnerStatus.RUNNING, '127.0.0.1', 50051) for model in models}
            yield lambda models: {model: (ModelRun.RunnerStatus.STOPPED, '127.0.0.1', 50051) for model in models}

        status_iterator = status_generator()
        self.runner.load_statuses = Mock(side_effect=lambda models: next(status_iterator)(models))

        # Stop mock:
        self.runner.stop = Mock(return_value=None)

        # Repository mock
        self.model_runs_repository_mock.save_model = MagicMock()
        self.model_runs_repository_mock.is_model_in_quarantine = Mock(return_value=False)

        self.state_subject_mock.notify_build_state_changed = MagicMock()
        self.state_subject_mock.notify_runner_state_changed = MagicMock()

        self.model_runs_service.start_model(model_id="mock_model_id",
                                            name="mock_name",
                                            cruncher_id="mock_cruncher_id",
                                            code_submission_id="mock_code_submission_id",
                                            resource_id="mock_resource_id",
                                            hardware_type=ModelRun.HardwareType.CPU,
                                            crunch=MagicMock())

        self.builder.build.assert_called()
        self.state_subject_mock.notify_build_state_changed.assert_called_with(ANY, None, ModelRun.BuilderStatus.BUILDING)

        # the model switch to build
        self.model_runs_service.updates_states()
        self.state_subject_mock.notify_build_state_changed.assert_called_with(ANY, ModelRun.BuilderStatus.BUILDING, ModelRun.BuilderStatus.SUCCESS)
        # The model switch to INITIALIZING after built
        self.runner.run.assert_called()
        self.state_subject_mock.notify_runner_state_changed.assert_called_with(ANY, None, ModelRun.RunnerStatus.INITIALIZING)

        self.augmented_model_run_repository_mock.loads = Mock(return_value={"mock_model_id": ModelInfo("tracker", "cruncher_abde")})

        # The model switch to running
        self.model_runs_service.updates_states()
        self.runner.load_statuses.assert_called()
        (model_run, prev_status, new_status) = self.state_subject_mock.notify_runner_state_changed.call_args.args
        self.assertEqual(model_run.ip, "127.0.0.1")
        self.assertEqual(model_run.port, 50051)
        self.assertEqual(model_run.augmented_info.name, "tracker")
        self.assertEqual(model_run.augmented_info.cruncher_name, "cruncher_abde")
        self.assertEqual(prev_status, ModelRun.RunnerStatus.INITIALIZING)
        self.assertEqual(new_status, ModelRun.RunnerStatus.RUNNING)

        # change the name
        self.augmented_model_run_repository_mock.loads = Mock(return_value={"mock_model_id": ModelInfo("tracker2", "cruncher_abdennour")})
        self.model_runs_service.updates_states()
        (model_run, prev_status, new_status) = self.state_subject_mock.notify_runner_state_changed.call_args.args
        self.assertEqual(model_run.ip, "127.0.0.1")
        self.assertEqual(model_run.port, 50051)
        self.assertEqual(model_run.augmented_info.name, "tracker2")
        self.assertEqual(model_run.augmented_info.cruncher_name, "cruncher_abdennour")
        self.assertEqual(prev_status, ModelRun.RunnerStatus.RUNNING)
        self.assertEqual(new_status, ModelRun.RunnerStatus.RUNNING)


        # Stop the model
        self.model_runs_service.stop_model(model_id="mock_model_id")
        self.runner.stop.assert_called()
        self.runner.load_statuses.assert_called()
        self.state_subject_mock.notify_runner_state_changed.assert_called_with(ANY, ModelRun.RunnerStatus.RUNNING, ModelRun.RunnerStatus.STOPPED)
        self.assertEqual(len(list(self.model_runs_service.cluster.models)), 1)

        # refresh status
        self.model_runs_service.updates_states()
        self.runner.load_statuses.assert_called()
        self.state_subject_mock.notify_runner_state_changed.assert_called_with(ANY, ModelRun.RunnerStatus.RUNNING, ModelRun.RunnerStatus.STOPPED)
        self.assertEqual(len(list(self.model_runs_service.cluster.models)), 0)

    def test_model_build_fail_scenario(self):
        self.builder.is_built = Mock(return_value=(False, None))
        self.builder.build = Mock(return_value=("job_id", "docker_image", "log_arn"))
        self.builder.load_statuses = Mock(side_effect=lambda models: {model: ModelRun.BuilderStatus.FAILED for model in models})

        # Repository mock
        self.model_runs_repository_mock.save_model = MagicMock()
        self.model_runs_repository_mock.is_model_in_quarantine = Mock(return_value=False)

        self.state_subject_mock.notify_build_state_changed = MagicMock()
        self.state_subject_mock.notify_failure = MagicMock()
        self.state_subject_mock.notify_runner_state_changed = MagicMock()

        self.model_runs_service.start_model(model_id="mock_model_id",
                                            name="mock_name",
                                            cruncher_id="mock_cruncher_id",
                                            code_submission_id="mock_code_submission_id",
                                            resource_id="mock_resource_id",
                                            hardware_type=ModelRun.HardwareType.CPU,
                                            crunch=MagicMock())

        self.builder.build.assert_called()
        self.state_subject_mock.notify_build_state_changed.assert_called_with(ANY, None, ModelRun.BuilderStatus.BUILDING)

        # the model switch to failed
        self.model_runs_service.updates_states()
        self.state_subject_mock.notify_build_state_changed.assert_called_with(ANY, ModelRun.BuilderStatus.BUILDING, ModelRun.BuilderStatus.FAILED)
        (model_run, failure) = self.state_subject_mock.notify_failure.call_args.kwargs.values()
        self.assertEqual(model_run.in_quarantine, True)
        self.assertEqual(model_run.failure.error_code, OrchestratorErrorType.BUILD_FAILED)
        self.assertEqual(model_run.failure.reason, OrchestratorErrorType.BUILD_FAILED.default_reason)
        self.assertEqual(model_run.failure, failure)

    def test_model_run_fail_if_model_in_quarantine_scenario(self):
        # Repository mock
        self.model_runs_repository_mock.save_model = MagicMock()
        self.model_runs_repository_mock.is_model_in_quarantine = Mock(return_value=True)

        self.model_runs_service.start_model(model_id="mock_model_id",
                                            name="mock_name",
                                            cruncher_id="mock_cruncher_id",
                                            code_submission_id="mock_code_submission_id",
                                            resource_id="mock_resource_id",
                                            hardware_type=ModelRun.HardwareType.CPU,
                                            crunch=MagicMock())

        self.state_subject_mock.notify_build_state_changed.assert_not_called()
        self.state_subject_mock.run_failure.assert_not_called()

        (model_run, failure) = self.state_subject_mock.notify_failure.call_args.kwargs.values()
        self.assertEqual(model_run.failure.error_code, OrchestratorErrorType.IN_QUARANTINE)
        self.assertEqual(model_run.failure.reason, OrchestratorErrorType.IN_QUARANTINE.default_reason)

        self.assertEqual(len(list(self.model_runs_service.cluster.models)), 0)

    def test_failure_receive_from_ws_client(self):
        def status_generator():
            yield lambda models: {model: (ModelRun.RunnerStatus.RUNNING, '127.0.0.1', 50051) for model in models}
            yield lambda models: {model: (ModelRun.RunnerStatus.STOPPED, '127.0.0.1', 50051) for model in models}

        status_iterator = status_generator()
        self.runner.load_statuses = Mock(side_effect=lambda models: next(status_iterator)(models))

        self.model_runs_service.cluster.add_model(
            ModelRun(
                id=None,
                model_id="mock_model_id",
                name="mock_name",
                crunch_id=MagicMock(),
                cruncher_onchain_info=CruncherOnchainInfo(wallet_pubkey="mock_cruncher_id", hotkey=""),
                code_submission_id="mock_code_submission_id",
                resource_id="mock_resource_id",
                hardware_type=ModelRun.HardwareType.CPU,
                desired_status=ModelRun.DesiredStatus.RUNNING,
                docker_image="mock_docker_image",
                runner_status=ModelRun.RunnerStatus.RUNNING,
                runner_job_id="mock_runner_job_id",
                ip="127.0.0.1",
            )
        )

        self.model_runs_service.report_model_runner_failure('BAD_IMPLEMENTATION', "mock_model_id", "127.0.0.1")

        # update
        self.model_runs_service.updates_states()

        self.state_subject_mock.notify_failure.assert_not_called()
        self.runner.stop.assser_called()
        self.state_subject_mock.notify_runner_state_changed.assert_called_with(ANY, ModelRun.RunnerStatus.RUNNING, ModelRun.RunnerStatus.STOPPED)

        self.model_runs_service.updates_states()
        self.state_subject_mock.notify_runner_state_changed.assert_called_with(ANY, ModelRun.RunnerStatus.RUNNING, ModelRun.RunnerStatus.STOPPED)
        (model_run, failure) = self.state_subject_mock.notify_failure.call_args.kwargs.values()
        self.assertEqual(model_run.in_quarantine, True)
        self.assertEqual(model_run.failure.error_code, ModelRunnerErrorType.BAD_IMPLEMENTATION)
        self.assertEqual(model_run.failure.reason, ModelRunnerErrorType.BAD_IMPLEMENTATION.default_reason)

        self.assertEqual(len(list(self.model_runs_service.cluster.models)), 0)

    def test_unexpected_stop(self):
        def status_generator():
            yield lambda models: {model: (ModelRun.RunnerStatus.RUNNING, '127.0.0.1', 50051) for model in models}
            yield lambda models: {model: (ModelRun.RunnerStatus.STOPPED, '127.0.0.1', 50051) for model in models}

        status_iterator = status_generator()
        self.runner.load_statuses = Mock(side_effect=lambda models: next(status_iterator)(models))

        self.model_runs_service.cluster.add_model(
            ModelRun(
                id=None,
                model_id="mock_model_id",
                name="mock_name",
                crunch_id=MagicMock(),
                cruncher_onchain_info=CruncherOnchainInfo(wallet_pubkey="mock_cruncher_id", hotkey=""),
                code_submission_id="mock_code_submission_id",
                resource_id="mock_resource_id",
                hardware_type=ModelRun.HardwareType.CPU,
                desired_status=ModelRun.DesiredStatus.RUNNING,
                docker_image="mock_docker_image",
                runner_status=ModelRun.RunnerStatus.RUNNING,
                runner_job_id="mock_runner_job_id",
                ip="127.0.0.1",
            )
        )

        # update
        self.model_runs_service.updates_states()

        self.state_subject_mock.notify_failure.assert_not_called()
        self.runner.stop.assert_not_called()
        self.state_subject_mock.notify_runner_state_changed.assert_not_called()

        # update with stop event
        self.model_runs_service.updates_states()

        self.state_subject_mock.notify_runner_state_changed.assert_called_with(ANY, ModelRun.RunnerStatus.RUNNING, ModelRun.RunnerStatus.STOPPED)
        (model_run, failure) = self.state_subject_mock.notify_failure.call_args.kwargs.values()
        self.assertEqual(model_run.runner_status, ModelRun.RunnerStatus.FAILED)
        self.assertEqual(model_run.failure.error_code, OrchestratorErrorType.STOP_UNEXPECTED)
        self.assertEqual(model_run.failure.reason, OrchestratorErrorType.STOP_UNEXPECTED.default_reason)
        self.assertEqual(model_run.in_quarantine, False)

        self.assertEqual(len(list(self.model_runs_service.cluster.models)), 0)

    def test_run_failed(self):
        self.runner.load_statuses = Mock(side_effect=lambda models: {model: (ModelRun.RunnerStatus.FAILED, None, None) for model in models})

        self.model_runs_service.cluster.add_model(
            ModelRun(
                id=None,
                model_id="mock_model_id",
                name="mock_name",
                crunch_id=MagicMock(),
                cruncher_onchain_info=CruncherOnchainInfo(wallet_pubkey="mock_cruncher_id", hotkey=""),
                code_submission_id="mock_code_submission_id",
                resource_id="mock_resource_id",
                hardware_type=ModelRun.HardwareType.CPU,
                desired_status=ModelRun.DesiredStatus.RUNNING,
                docker_image="mock_docker_image",
                runner_status=ModelRun.RunnerStatus.RUNNING,
                runner_job_id="mock_runner_job_id",
                ip="127.0.0.1",
            )
        )

        # update
        self.model_runs_service.updates_states()

        self.state_subject_mock.notify_runner_state_changed.assert_called_with(ANY, ModelRun.RunnerStatus.RUNNING, ModelRun.RunnerStatus.FAILED)
        (model_run, failure) = self.state_subject_mock.notify_failure.call_args.kwargs.values()
        self.assertEqual(model_run.in_quarantine, True)
        self.assertEqual(model_run.failure.error_code, OrchestratorErrorType.RUN_FAILED)
        self.assertEqual(model_run.failure.reason, OrchestratorErrorType.RUN_FAILED.default_reason)

        self.assertEqual(len(list(self.model_runs_service.cluster.models)), 0)

    def test_run_aws_failed(self):
        self.builder.is_built = Mock(return_value=(True, "docker_image"))
        self.model_runs_repository_mock.is_model_in_quarantine = Mock(return_value=False)
        self.runner.run = Mock(side_effect=Exception("AWS failed to run"))

        self.model_runs_service.start_model(model_id="mock_model_id",
                                            name="mock_name",
                                            cruncher_id="mock_cruncher_id",
                                            code_submission_id="mock_code_submission_id",
                                            resource_id="mock_resource_id",
                                            hardware_type=ModelRun.HardwareType.CPU,
                                            crunch=MagicMock())

        self.state_subject_mock.notify_runner_state_changed.assert_not_called()
        (model_run, failure) = self.state_subject_mock.notify_failure.call_args.kwargs.values()
        self.assertEqual(model_run.runner_status, ModelRun.RunnerStatus.FAILED)
        self.assertEqual(model_run.failure.error_code, CloudProviderErrorType.RUN_EXCEPTION)
        self.assertEqual(model_run.failure.reason, CloudProviderErrorType.RUN_EXCEPTION.default_reason)
        self.assertIsNotNone(model_run.failure.exception)
        self.assertIsNotNone(model_run.failure.traceback)
        self.assertEqual(len(list(self.model_runs_service.cluster.models)), 0)

    def test_build_aws_failed(self):
        self.builder.is_built = Mock(side_effect=Exception("AWS failed to check build status"))
        self.model_runs_repository_mock.is_model_in_quarantine = Mock(return_value=False)

        self.builder.build = Mock(side_effect=Exception("AWS failed to build"))

        self.model_runs_service.start_model(model_id="mock_model_id",
                                            name="mock_name",
                                            cruncher_id="mock_cruncher_id",
                                            code_submission_id="mock_code_submission_id",
                                            resource_id="mock_resource_id",
                                            hardware_type=ModelRun.HardwareType.CPU,
                                            crunch=MagicMock())

        self.state_subject_mock.notify_build_state_changed.assert_not_called()
        self.builder.build.assert_called()

        (model_run, failure) = self.state_subject_mock.notify_failure.call_args.kwargs.values()
        self.assertEqual(model_run.runner_status, ModelRun.RunnerStatus.FAILED)
        self.assertEqual(model_run.failure.error_code, CloudProviderErrorType.BUILD_EXCEPTION)
        self.assertEqual(model_run.failure.reason, CloudProviderErrorType.BUILD_EXCEPTION.default_reason)
        self.assertIsNotNone(model_run.failure.exception)
        self.assertIsNotNone(model_run.failure.traceback)

        self.assertEqual(len(list(self.model_runs_service.cluster.models)), 0)

    def test_stop_aws_failed(self):
        self.runner.stop = Mock(side_effect=Exception("AWS failed to load statuses"))

        self.model_runs_service.cluster.add_model(
            ModelRun(
                id=None,
                model_id="mock_model_id",
                name="mock_name",
                crunch_id=MagicMock(),
                cruncher_onchain_info=CruncherOnchainInfo(wallet_pubkey="mock_cruncher_id", hotkey=""),
                code_submission_id="mock_code_submission_id",
                resource_id="mock_resource_id",
                hardware_type=ModelRun.HardwareType.CPU,
                desired_status=ModelRun.DesiredStatus.RUNNING,
                docker_image="mock_docker_image",
                runner_status=ModelRun.RunnerStatus.RUNNING,
                runner_job_id="mock_runner_job_id",
                ip="127.0.0.1",
            )
        )

        self.model_runs_service.stop_model(model_id="mock_model_id")
        self.state_subject_mock.notify_runner_state_changed.assert_called_with(ANY, ModelRun.RunnerStatus.RUNNING, ModelRun.RunnerStatus.STOPPED)

        (model_run, failure) = self.state_subject_mock.notify_failure.call_args.kwargs.values()
        self.assertEqual(model_run.failure.error_code, CloudProviderErrorType.RUN_EXCEPTION)
        self.assertEqual(model_run.failure.reason, CloudProviderErrorType.RUN_EXCEPTION.default_reason)
        self.assertIsNotNone(model_run.failure.exception)
        self.assertIsNotNone(model_run.failure.traceback)
        self.assertEqual(len(list(self.model_runs_service.cluster.models)), 0)


if __name__ == '__main__':
    unittest.main()
