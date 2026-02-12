"""
Phala TEE Model Runner.

Delegates model execution to the remote spawntee service via HTTP.
The spawntee starts pre-built Docker containers inside the TEE and
exposes their gRPC services on dynamically allocated ports.
"""

from ...configuration.properties._infrastructure import PhalaRunnerInfrastructureConfig
from ...entities import ModelRun
from ...entities.crunch import Crunch
from ...services import Runner
from ...utils.logging_utils import get_logger
from ._client import SpawnteeClient, SpawnteeClientError
from ._metrics import PhalaMetrics

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

    def __init__(self, config: PhalaRunnerInfrastructureConfig, metrics: PhalaMetrics | None = None):
        super().__init__()
        self._config = config
        self._metrics = metrics
        self._clients = [
            SpawnteeClient(
                cluster_url_template=url,
                spawntee_port=config.spawntee_port,
                timeout=config.request_timeout,
            )
            for url in config.cluster_urls
        ]

    @property
    def _client(self) -> SpawnteeClient:
        """Return the first spawntee client (single-cluster for now)."""
        return self._clients[0]

    def create(self, crunch: Crunch) -> dict:
        """No infrastructure to create — the CVM is already running."""
        return {}

    def run(self, model: ModelRun, crunch: Crunch) -> tuple[str, str, dict]:
        """
        Start a pre-built model container on the spawntee.

        The model must have been built first (builder_job_id is the spawntee task_id).
        The spawntee will start the container from the pre-built image.
        Resource limits (memory, CPU) are read from the crunch's infrastructure
        config — same as AWS ECS.

        Args:
            model: ModelRun whose builder_job_id is the spawntee task_id.
            crunch: Crunch configuration with infrastructure.cpu_config limits.

        Returns:
            Tuple of (task_id, logs_arn, runner_info).
        """
        task_id = model.builder_job_id
        if not task_id:
            raise ValueError(f"Model {model.model_id} has no builder_job_id — was it built?")

        # Extract resource limits from crunch config (same source as AWS ECS)
        memory_mb = None
        cpu_vcpus = None
        if crunch.infrastructure and crunch.infrastructure.cpu_config:
            memory_mb = crunch.infrastructure.cpu_config.memory
            cpu_vcpus = crunch.infrastructure.cpu_config.vcpus

        logger.info(
            "Starting model on spawntee: task_id=%s, model=%s, memory=%sMB, cpu=%svCPU",
            task_id, model.model_id, memory_mb, cpu_vcpus,
        )

        try:
            result = self._client.start_model(task_id, memory_mb=memory_mb, cpu_vcpus=cpu_vcpus)
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

        Each model's runner_job_id is the spawntee task_id.
        When the start-model task completes, the task metadata contains
        the container's port, which we use to construct the external gRPC URL.
        """
        result = {}
        for model in models:
            task_id = model.runner_job_id
            if not task_id:
                result[model] = (ModelRun.RunnerStatus.FAILED, '', 0)
                continue

            try:
                task = self._client.get_task(task_id)
                spawntee_status = task.get("status", "failed")
                status = _STATUS_MAP.get(spawntee_status, ModelRun.RunnerStatus.FAILED)

                # Record metrics for completed/failed operations
                if self._metrics and spawntee_status in ("completed", "failed"):
                    self._metrics.record_from_task(task)

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

            except SpawnteeClientError as e:
                logger.warning("Failed to poll run status for task %s: %s", task_id, e)
                # On transient errors, keep current status
                current_status = model.runner_status or ModelRun.RunnerStatus.INITIALIZING
                result[model] = (current_status, model.ip or '', model.port or 0)

        return result

    def stop(self, model: ModelRun) -> str:
        """
        Stop a running model container on the spawntee.

        The spawntee handles the async cleanup (stop container, remove network,
        clean up model data, prune Docker resources).
        """
        task_id = model.runner_job_id
        if not task_id:
            logger.warning("Cannot stop model %s — no runner_job_id", model.model_id)
            return ""

        logger.info("Stopping model on spawntee: task_id=%s, model=%s", task_id, model.model_id)

        try:
            result = self._client.stop_model(task_id)
            logger.info("Stop submitted: task_id=%s for model=%s", task_id, model.model_id)
            return result.get("task_id", task_id)
        except SpawnteeClientError as e:
            logger.error("Failed to stop model %s (task %s): %s", model.model_id, task_id, e)
            raise

    def _extract_grpc_address(self, task: dict) -> tuple[str, int]:
        """
        Extract the external gRPC hostname and port from a spawntee task.

        Prefers external_grpc_hostname/external_grpc_port from task metadata
        (set by newer spawntee versions that include the full hostname with
        gRPC routing suffix). Falls back to constructing the URL from the
        port template for legacy spawntee deployments.
        """
        metadata = task.get("metadata") or {}
        container = metadata.get("container") or {}

        # Prefer external hostname from spawntee (includes g suffix)
        external_hostname = container.get("external_grpc_hostname")
        external_port = container.get("external_grpc_port", 443)
        if external_hostname:
            logger.debug("Model gRPC endpoint (from metadata): %s:%d", external_hostname, external_port)
            return external_hostname, external_port

        # Fallback: construct from URL template (legacy spawntee without external hostname)
        model_port = container.get("port")
        if not model_port:
            logger.warning("Task %s has no container port in metadata", task.get("task_id"))
            return '', 0

        model_port = int(model_port)
        hostname, port = self._client.model_grpc_url(model_port)
        logger.debug("Model gRPC endpoint (from template): %s:%d (CVM port %d)", hostname, port, model_port)
        return hostname, port
