import json
from zoneinfo import ZoneInfo

from model_orchestrator.utils.logging_utils import get_logger

from ....entities import (CpuConfig, Crunch, GpuConfig, Infrastructure,
                          RunnerType)
from ....entities.crunch import RunSchedule, CoordinatorInfo
from ....repositories import CrunchRepository
from ._base import SQLiteRepository


class SQLiteCrunchRepository(SQLiteRepository, CrunchRepository):
    """
    A Repository implementation for SQLite.
    """

    def __init__(self, db_path: str):
        SQLiteRepository.__init__(self, db_path)

        self.create_table()

    def create_table(self):
        """
        Creates the 'models' table if it does not exist.
        """

        with self._open() as db:
            get_logger().debug(f"Creating table 'crunches' in {self.db_path}")
            db["crunches"].create(
                {
                    "id": str,
                    "name": str,
                    "infra_zone": str,
                    "runner_type": str,
                    "cpu_vcpus": int,
                    "cpu_memory": int,
                    "cpu_instance_types": str,
                    "gpu_vcpus": int,
                    "gpu_memory": int,
                    "gpu_instance_types": str,
                    "gpus_per_instance": int,
                    "runner_config": str,
                    "network_config": str,
                    "builder_config": str,
                },
                pk="id",
                if_not_exists=True,
            )

            self._add_column_if_not_exists(db, 'crunches', 'run_schedule_intervals', str)
            self._add_column_if_not_exists(db, 'crunches', 'run_schedule_tz', str)
            self._add_column_if_not_exists(db, 'crunches', 'cluster_name', str)
            self._add_column_if_not_exists(db, 'crunches', 'onchain_name', str)
            self._add_column_if_not_exists(db, 'crunches', 'is_secure', bool)
            self._add_column_if_not_exists(db, 'crunches', 'onchain_address', str)
            self._add_column_if_not_exists(db, 'crunches', 'coordinator_wallet_pubkey', str)
            self._add_column_if_not_exists(db, 'crunches', 'coordinator_hotkey', str)

    def save(self, crunch: Crunch):
        with self._open() as db:
            db["crunches"].upsert(
                {
                    "id": crunch.id,
                    "name": crunch.name,
                    "onchain_name": crunch.onchain_name,
                    "cluster_name": crunch.infrastructure.cluster_name,
                    "infra_zone": crunch.infrastructure.zone,
                    "is_secure": crunch.infrastructure.is_secure,
                    "runner_type": crunch.infrastructure.runner_type.value,
                    "cpu_vcpus": crunch.infrastructure.cpu_config.vcpus if crunch.infrastructure.cpu_config else None,
                    "cpu_memory": crunch.infrastructure.cpu_config.memory if crunch.infrastructure.cpu_config else None,
                    "cpu_instance_types": json.dumps(crunch.infrastructure.cpu_config.instances_types) if crunch.infrastructure.cpu_config else None,
                    "gpu_vcpus": crunch.infrastructure.gpu_config.vcpus if crunch.infrastructure.gpu_config else None,
                    "gpu_memory": crunch.infrastructure.gpu_config.memory if crunch.infrastructure.gpu_config else None,
                    "gpu_instance_types": json.dumps(crunch.infrastructure.gpu_config.instances_types) if crunch.infrastructure.gpu_config else None,
                    "gpus_per_instance": crunch.infrastructure.gpu_config.gpus if crunch.infrastructure.gpu_config else None,
                    "runner_config": json.dumps(crunch.runner_config),
                    "network_config": json.dumps(crunch.network_config),
                    "builder_config": json.dumps(crunch.builder_config),
                    "run_schedule_intervals": crunch.run_schedule.intervals if crunch.run_schedule else None,
                    "run_schedule_tz": str(crunch.run_schedule.timezone) if crunch.run_schedule and crunch.run_schedule.timezone else None,
                    "onchain_address": crunch.onchain_address,
                    "coordinator_wallet_pubkey": crunch.coordinator_info.wallet_pubkey if crunch.coordinator_info else None,
                    "coordinator_hotkey": crunch.coordinator_info.hotkey if crunch.coordinator_info else None,
                },
                pk="id"
            )

    def delete(self, crunch_id: str):
        """
        Deletes a model by ID.
        """

        with self._open() as db:
            db["crunches"].delete(crunch_id)

    def get(self, id: str) -> Crunch:
        """
        Fetches the details of a model by its ID.
        Returns a dictionary containing the model details.
        """

        with self._open() as db:
            row = db["crunches"].get(id)
            if row is None:
                return None

            return self._from_values(row)

    def load_active(self):
        """
        Fetches all active Crunch records from the database.
        Returns:
            A dictionary where keys are Crunch IDs and values are Crunch objects.
        """

        with self._open() as db:
            rows = db["crunches"].rows

            return [
                self._from_values(row)
                for row in rows
            ]

    @staticmethod
    def _from_values(values) -> Crunch:
        """
        Creates a Crunch object from database row values.
        """

        return Crunch(
            id=values["id"],
            name=values["name"],
            onchain_name=values["onchain_name"],
            infrastructure=Infrastructure(
                cluster_name=values["cluster_name"],
                zone=values["infra_zone"],
                is_secure=values["is_secure"],
                runner_type=RunnerType(values["runner_type"]),
                cpu_config=CpuConfig(
                    vcpus=values["cpu_vcpus"],
                    memory=values["cpu_memory"],
                    instances_types=json.loads(values["cpu_instance_types"]) if values["cpu_instance_types"] else None
                ) if values["cpu_vcpus"] is not None else None,
                gpu_config=GpuConfig(
                    vcpus=values["gpu_vcpus"],
                    memory=values["gpu_memory"],
                    instances_types=json.loads(values["gpu_instance_types"]) if values["gpu_instance_types"] else None,
                    gpus=values["gpus_per_instance"]
                ) if values["gpu_vcpus"] is not None else None
            ),
            runner_config=json.loads(values["runner_config"]),
            network_config=json.loads(values["network_config"]),
            builder_config=json.loads(values["builder_config"]),
            run_schedule=None if values["run_schedule_intervals"] is None else RunSchedule(
                values["run_schedule_intervals"],
                ZoneInfo(values["run_schedule_tz"]) if values["run_schedule_tz"] else None
            ),
            onchain_address=values["onchain_address"],
            coordinator_info=None if values["coordinator_wallet_pubkey"] is None else CoordinatorInfo(wallet_pubkey=values["coordinator_wallet_pubkey"], hotkey=values["coordinator_hotkey"])
        )


if __name__ == "__main__":
    import os
    from pathlib import Path

    file_path = Path(__file__).parent
    DATA_PATH = os.path.join(file_path, "../../../data/orchestrator.db")
    repository = SQLiteCrunchRepository(DATA_PATH)

    print(repository.get('bird-game'))
