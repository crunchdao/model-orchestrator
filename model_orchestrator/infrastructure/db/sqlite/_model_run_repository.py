import datetime
import json
from dataclasses import asdict
from typing import Any, Optional

import newrelic.agent
import sqlite_utils

from ....entities import Failure, ModelRun, ModelInfo
from ....entities.errors import deserialize_error_type, serialize_error_type
from ....entities import CruncherOnchainInfo
from ....repositories.model_run_repository import ModelRunRepository
from ....utils.logging_utils import get_logger
from ._base import SQLiteRepository


class SQLiteModelRunRepository(ModelRunRepository, SQLiteRepository):
    """
    A Repository implementation for SQLite using sqlite-utils.
    """

    def __init__(self, db_path: str):
        SQLiteRepository.__init__(self, db_path)

        self.create_table()

    def _add_column_if_not_exists(self, db: sqlite_utils.Database, column_name: str, column_type: Optional[Any]):
        if column_name not in db['model_runs'].columns_dict:
            db['model_runs'].add_column(column_name, column_type)

    @newrelic.agent.datastore_trace("sqlite", "model_runs", "create")
    def create_table(self):
        """
        Creates the 'model_runs' table if it does not exist.
        """
        with self._open() as db:
            get_logger().debug("Creating table 'model_runs' in %s", self.db_path)
            db["model_runs"].create({
                "id": str,
                "model_id": str,
                "name": str,
                "crunch_id": str,
                "code_submission_id": str,
                "resource_id": str,
                "hardware_type": str,
                "desired_status": str,
                "docker_image": str,
                "runner_status": str,
                "builder_status": str,
                "runner_job_id": str,
                "builder_job_id": str,
                "created_at": str,
                "last_update": str,
                "runner_info": str,
                "ip": str,
                "port": int,
                "runner_logs_arn": str,
                "builder_logs_arn": str
            }, pk="id", if_not_exists=True)

            self._add_column_if_not_exists(db, 'cruncher_id', str)
            self._add_column_if_not_exists(db, 'failure_error_code', str)
            self._add_column_if_not_exists(db, 'failure_reason', str)
            self._add_column_if_not_exists(db, 'failure_exception', str)
            self._add_column_if_not_exists(db, 'failure_traceback', str)
            self._add_column_if_not_exists(db, 'failure_occurred_at', str)
            self._add_column_if_not_exists(db, 'in_quarantine', bool)
            self._add_column_if_not_exists(db, 'augmented_info', str)
            self._add_column_if_not_exists(db, 'cruncher_wallet_pubkey', str)
            self._add_column_if_not_exists(db, 'cruncher_hotkey', str)

    @newrelic.agent.datastore_trace("sqlite", "model_runs", "select")
    def load_active(self) -> list[ModelRun]:
        """
        Loads all model_runs that are NOT (STOPPED/STOPPED).
        Returns a dict of {model_name: Model}.
        """
        with self._open() as db:
            active_runs = db["model_runs"].rows_where(
                "NOT (runner_status = ? OR runner_status = ?)",  # quick fix, need to change to use in instruction
                [ModelRun.RunnerStatus.STOPPED.value, ModelRun.RunnerStatus.FAILED.value]
            )
            models: list[ModelRun] = []
            for row in active_runs:
                model = self.build_model_run_from_row(row)
                models.append(model)
            return models

    @newrelic.agent.datastore_trace("sqlite", "model_runs", "select")
    def load_all_latest(self) -> list[ModelRun]:
        with self._open() as db:
            rows = db.query("""
                SELECT * FROM model_runs as A
                WHERE datetime(A.created_at) = (
                    SELECT max(datetime(B.created_at)) 
                    FROM model_runs as B 
                    WHERE B.model_id = A.model_id
                )
            """)
            models: list[ModelRun] = []
            for row in rows:
                model = self.build_model_run_from_row(row)
                models.append(model)
            return models

    @newrelic.agent.datastore_trace("sqlite", "model_runs", "upsert")
    def save_model(self, model: ModelRun) -> None:
        """
        Insert or update the given model in the SQLite database using sqlite-utils.
        """
        model_data = {
            "id": model.id,
            "model_id": model.model_id,
            "name": model.name,
            "crunch_id": model.crunch_id,
            "cruncher_id": model.cruncher_id,
            "code_submission_id": model.code_submission_id,
            "resource_id": model.resource_id,
            "hardware_type": model.hardware_type.value,
            "desired_status": model.desired_status.value,
            "docker_image": model.docker_image,
            "runner_status": model.runner_status.value if model.runner_status else None,
            "builder_status": model.builder_status.value if model.builder_status else None,
            "runner_job_id": model.runner_job_id,
            "builder_job_id": model.builder_job_id,
            "created_at": model.created_at.isoformat(),
            "last_update": model.last_update.isoformat(),
            "runner_info": str(model.runner_info),
            "ip": model.ip,
            "port": model.port,
            "runner_logs_arn": model.runner_logs_arn,
            "builder_logs_arn": model.builder_logs_arn,
            'failure_error_code': serialize_error_type(model.failure.error_code) if model.failure else None,
            'failure_reason': model.failure.reason if model.failure else None,
            'failure_exception': str(model.failure.exception) if model.failure else None,
            'failure_traceback': model.failure.traceback if model.failure else None,
            'failure_occurred_at': model.failure.occurred_at.isoformat() if model.failure else None,
            'in_quarantine': 1 if model.in_quarantine else 0,
            'augmented_info': json.dumps(asdict(model.augmented_info)) if model.augmented_info else None,
            'cruncher_wallet_pubkey': model.cruncher_onchain_info.wallet_pubkey,
            'cruncher_hotkey': model.cruncher_onchain_info.hotkey,
        }

        with self._open() as db:
            db["model_runs"].upsert(model_data, pk="id")

    @newrelic.agent.datastore_trace("sqlite", "model_runs", "delete")
    def delete_model(self, model_id: str) -> None:
        """
        Removes a model from the table using its ID.
        """

        with self._open() as db:
            db["model_runs"].delete(model_id)

    @newrelic.agent.datastore_trace("sqlite", "model_runs", "select")
    def is_model_in_quarantine(self, model_id: str, code_submission_id: str, resource_id: str, hardware_type: ModelRun.HardwareType) -> bool:
        """
        Fetches a quarantined ModelRun based on provided attributes.
        """

        with self._open() as db:
            count = db["model_runs"].count_where(
                "model_id = ? AND code_submission_id = ? AND resource_id = ? AND hardware_type = ? AND in_quarantine = ?",
                [model_id, code_submission_id, resource_id, hardware_type.value, 1]
            )

            return count > 0

    def build_model_run_from_row(self, row) -> ModelRun:
        desired_enum = ModelRun.DesiredStatus(row["desired_status"])
        runner_status_enum = ModelRun.RunnerStatus(row["runner_status"]) if row["runner_status"] else None
        builder_status_enum = ModelRun.BuilderStatus(row["builder_status"]) if row["builder_status"] else None
        hardware_type_enum = ModelRun.HardwareType(row["hardware_type"])

        created_at = datetime.datetime.fromisoformat(row["created_at"]) if row["created_at"] else None
        last_update = datetime.datetime.fromisoformat(row["last_update"]) if row["last_update"] else None

        runner_info = eval(row["runner_info"]) if row["runner_info"] else {}

        return ModelRun(
            id=row["id"],
            model_id=row["model_id"],
            name=row["name"],
            crunch_id=row["crunch_id"],
            cruncher_onchain_info=CruncherOnchainInfo(wallet_pubkey=row["cruncher_wallet_pubkey"], hotkey=row["cruncher_hotkey"]),
            code_submission_id=row["code_submission_id"],
            resource_id=row["resource_id"],
            hardware_type=hardware_type_enum,
            desired_status=desired_enum,
            docker_image=row["docker_image"],
            runner_status=runner_status_enum,
            builder_status=builder_status_enum,
            runner_job_id=row["runner_job_id"],
            builder_job_id=row["builder_job_id"],
            created_at=created_at,
            last_update=last_update,
            runner_info=runner_info,
            ip=row["ip"],
            port=row["port"],
            runner_logs_arn=row["runner_logs_arn"],
            builder_logs_arn=row["builder_logs_arn"],
            failure=Failure(
                model_run_id=row["id"],
                error_code=deserialize_error_type(row["failure_error_code"]),
                reason=row["failure_reason"],
                exception=row["failure_exception"],
                traceback=row["failure_traceback"],
                occurred_at=datetime.datetime.fromisoformat(row["failure_occurred_at"]) if row["failure_occurred_at"] else None,
            ) if row["failure_error_code"] else None,
            in_quarantine=row["in_quarantine"] == 1,
            augmented_info=ModelInfo(**json.loads(row["augmented_info"])) if row["augmented_info"] else None
        )