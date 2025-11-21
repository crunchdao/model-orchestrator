import datetime
import traceback
import unittest
import os

import sqlite_utils

from model_orchestrator.entities import ErrorType, OrchestratorError, OrchestratorErrorType
from model_orchestrator.entities.model_run import ModelRun
from model_orchestrator.entities.failure import Failure
from model_orchestrator.infrastructure.db.sqlite import SQLiteModelRunRepository


class TestSQLiteModelRunRepository(unittest.TestCase):

    def setUp(self):
        """
        Initialize an in-memory SQLite database for testing.
        """
        self.db_path = "/tmp/test_sqlite_model_run_repository.db"
        self.repository = SQLiteModelRunRepository(self.db_path)

    def tearDown(self):
        """
        Remove the test database file after test execution.
        """
        if os.path.exists(self.db_path):
            os.remove(self.db_path)

    def test_save_and_load_model_run(self):
        """
        Test inserting a ModelRun instance and retrieving it to verify correct storage.
        """
        try:
            raise ValueError("Simulate exception")
        except ValueError as e:
            exception = e
            except_traceback = traceback.format_exc()  # Capture la traceback en string
            pass

        model_run = ModelRun(
            id="test_id",
            model_id="1",
            name="Test Model",
            crunch_id="crunch_001",
            cruncher_id="cruncher_001",
            code_submission_id="code_sub_001",
            resource_id="res_001",
            hardware_type=ModelRun.HardwareType.CPU,
            desired_status=ModelRun.DesiredStatus.RUNNING,
            docker_image="example_image:latest",
            runner_status=ModelRun.RunnerStatus.RUNNING,
            builder_status=ModelRun.BuilderStatus.SUCCESS,
            runner_job_id="runner_job_001",
            builder_job_id="builder_job_001",
            created_at=datetime.datetime.now(),
            last_update=datetime.datetime.now(),
            runner_info={"info": "sample runner info"},
            ip="127.0.0.1",
            port=8080,
            runner_logs_arn="arn:aws:logs:region:123456789012:log-group:runner",
            builder_logs_arn="arn:aws:logs:region:123456789012:log-group:builder",
            failure=Failure(
                model_run_id="test_id",
                error_code=OrchestratorErrorType.RUN_FAILED,
                reason="Sample Failure",
                exception=exception,
                traceback=except_traceback,
                occurred_at=datetime.datetime.now()
            ),
            in_quarantine=False
        )

        # Save model
        self.repository.save_model(model_run)

        # Retrieve model and rebuild from row
        db = sqlite_utils.Database(self.db_path)
        row = db["model_runs"].get(model_run.id)
        self.assertIsNotNone(row)
        retrieved_model = self.repository.build_model_run_from_row(row)

        self.assertEqual(retrieved_model.id, model_run.id)
        self.assertEqual(retrieved_model.model_id, model_run.model_id)
        self.assertEqual(retrieved_model.name, model_run.name)
        self.assertEqual(retrieved_model.crunch_id, model_run.crunch_id)
        self.assertEqual(retrieved_model.runner_status, model_run.runner_status)
        self.assertEqual(retrieved_model.port, model_run.port)
        self.assertEqual(retrieved_model.failure.error_code, model_run.failure.error_code)
        self.assertEqual(retrieved_model.failure.reason, model_run.failure.reason)
        self.assertEqual(retrieved_model.failure.traceback, model_run.failure.traceback)
        self.assertEqual(retrieved_model.in_quarantine, model_run.in_quarantine)
        self.assertIsNotNone(retrieved_model.failure.exception)


if __name__ == "__main__":
    unittest.main()
