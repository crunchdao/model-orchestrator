"""
Phala TEE Model Runner.

Delegates model execution to the remote spawntee service via HTTP.
The spawntee starts pre-built Docker containers inside the TEE and
exposes their gRPC services on dynamically allocated ports.

Uses PhalaCluster for CVM routing:
- run/stop/status operations route to the CVM that owns the task
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ...entities import ModelRun
from ...entities.crunch import Crunch
from ...services import Runner
from ...utils.logging_utils import get_logger
from ._client import SpawnteeAuthenticationError, SpawnteeClientError

if TYPE_CHECKING:
    from ._cluster import PhalaCluster

logger = get_logger()

# Spawntee task status → orchestrator RunnerStatus mapping
_STATUS_MAP = {
    "pending": ModelRun.RunnerStatus.INITIALIZING,
    "running": ModelRun.RunnerStatus.INITIALIZING,  # task still executing
    "completed": ModelRun.RunnerStatus.RUNNING,      # start task completed → container is running
    "failed": ModelRun.RunnerStatus.FAILED,
}


class PhalaModelRunner(Runner):
    """
    Runner that calls the Phala spawntee /start-model and /stop-model APIs.

    Model containers run inside the TEE's Docker-in-Docker environment.
    The orchestrator communicates with them via gRPC over the CVM's
    externally-mapped ports.
    """

    def __init__(self, cluster: PhalaCluster):
        super().__init__()
        self._cluster = cluster

    def create(self, crunch: Crunch) -> dict:
        """No infrastructure to create — the CVM is already running."""
        return {}

    def run(self, model: ModelRun, crunch: Crunch) -> tuple[str, str, dict]:
        """
        Start a pre-built model container on the spawntee.

        Routes to the CVM that built the model (via task_id → CVM mapping).

        Args:
            model: ModelRun whose builder_job_id is the spawntee task_id.
            crunch: Crunch configuration with infrastructure.cpu_config limits.

        Returns:
            Tuple of (task_id, logs_arn, runner_info).
        """
        task_id = model.builder_job_id
        if not task_id:
            raise ValueError(f"Model {model.model_id} has no builder_job_id — was it built?")

        # Route to the CVM that owns this task
        client = self._cluster.client_for_task(task_id)

        # Resource limits: use the cluster's per-model memory budget (from
        # orchestrator config memory-per-model-mb).  Fall back to cpu_config
        # if the crunch has one (AWS ECS compat), but the cluster value is
        # the canonical source for Phala deployments.
        memory_mb = self._cluster.memory_per_model_mb
        cpu_vcpus = None
        if crunch.infrastructure and crunch.infrastructure.cpu_config:
            memory_mb = crunch.infrastructure.cpu_config.memory or memory_mb
            cpu_vcpus = crunch.infrastructure.cpu_config.vcpus

        logger.info(
            "Starting model on spawntee: task_id=%s, model=%s, memory=%sMB, cpu=%svCPU",
            task_id, model.model_id, memory_mb, cpu_vcpus,
        )

        try:
            result = client.start_model(task_id, memory_mb=memory_mb, cpu_vcpus=cpu_vcpus)
        except SpawnteeClientError as e:
            logger.error("Failed to start model %s (task %s): %s", model.model_id, task_id, e)
            raise

        logger.info("Start submitted: task_id=%s for model=%s", task_id, model.model_id)

        logs_arn = None  # Logs live inside the TEE
        runner_info = {"spawntee_task_id": task_id}
        return task_id, logs_arn, runner_info

    def load_status(self, model: ModelRun) -> tuple[ModelRun.RunnerStatus, str, int]:
        """Poll the spawntee for a single model's run status."""
        return self.load_statuses([model])[model]

    def load_statuses(self, models: list[ModelRun]) -> dict[ModelRun, tuple[ModelRun.RunnerStatus, str, int]]:
        """
        Poll the spawntee for run statuses of the given models.

        Routes each poll to the correct CVM via the cluster's task map.
        """
        result = {}
        for model in models:
            task_id = model.runner_job_id
            if not task_id:
                raise ValueError(
                    f"Model {model.model_id} has no runner_job_id — cannot poll status"
                )

            client = self._cluster.client_for_task(task_id)
            try:
                task = client.get_task(task_id)
                spawntee_status = task.get("status")
                if spawntee_status is None:
                    raise SpawnteeClientError(
                        f"Spawntee /task response missing 'status' field: {task}"
                    )
                status = _STATUS_MAP.get(spawntee_status)
                if status is None:
                    raise SpawnteeClientError(
                        f"Spawntee returned unknown runner task status: {spawntee_status!r}"
                    )

                ip = ''
                port = 0

                if status == ModelRun.RunnerStatus.RUNNING:
                    ip, port = self._extract_grpc_address(task)

                # Check if the model was actually stopped via stop operation
                if spawntee_status == "completed":
                    current_op = task.get("current_operation")
                    if current_op == "stop_model":
                        status = ModelRun.RunnerStatus.STOPPED
                        ip = ''
                        port = 0

                result[model] = (status, ip, port)

            except SpawnteeAuthenticationError:
                raise  # critical — do not swallow
            except SpawnteeClientError as e:
                logger.warning("Failed to poll run status for task %s: %s", task_id, e)
                # On transient errors, keep current status
                current_status = model.runner_status or ModelRun.RunnerStatus.INITIALIZING
                result[model] = (current_status, model.ip or '', model.port or 0)

        return result

    def stop(self, model: ModelRun) -> str:
        """
        Stop a running model container on the spawntee.

        Routes to the CVM that owns this task.
        """
        task_id = model.runner_job_id
        if not task_id:
            raise ValueError(
                f"Model {model.model_id} has no runner_job_id — cannot stop"
            )

        client = self._cluster.client_for_task(task_id)

        logger.info("Stopping model on spawntee: task_id=%s, model=%s", task_id, model.model_id)

        try:
            result = client.stop_model(task_id)
            if "task_id" not in result:
                raise SpawnteeClientError(
                    f"Spawntee /stop-model response missing 'task_id' field: {result}"
                )
            logger.info("Stop submitted: task_id=%s for model=%s", task_id, model.model_id)
            return result["task_id"]
        except SpawnteeClientError as e:
            logger.error("Failed to stop model %s (task %s): %s", model.model_id, task_id, e)
            raise

    def _extract_grpc_address(self, task: dict) -> tuple[str, int]:
        """
        Extract the external gRPC hostname and port from a spawntee task.

        Requires external_grpc_hostname and external_grpc_port in the task's
        container metadata. Raises SpawnteeClientError if missing.
        """
        task_id = task.get("task_id", "?")
        metadata = task.get("metadata")
        if not metadata or not metadata.get("container"):
            raise SpawnteeClientError(
                f"Task {task_id} is RUNNING but has no container metadata: {task}"
            )
        container = metadata["container"]

        hostname = container.get("external_grpc_hostname")
        port = container.get("external_grpc_port")
        if not hostname or port is None:
            raise SpawnteeClientError(
                f"Task {task_id} container metadata missing "
                f"external_grpc_hostname or external_grpc_port: {container}"
            )

        logger.debug("Model gRPC endpoint: %s:%d", hostname, port)
        return hostname, int(port)
