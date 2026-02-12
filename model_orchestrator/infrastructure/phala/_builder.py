"""
Phala TEE Model Builder.

Delegates model building to the remote spawntee service via HTTP.
The spawntee downloads encrypted model files from S3, decrypts them
inside the TEE, and builds a Docker image — all remotely.
"""

from ...configuration.properties._infrastructure import PhalaRunnerInfrastructureConfig
from ...entities import ModelRun
from ...entities.crunch import Crunch
from ...services import Builder
from ...utils.logging_utils import get_logger
from ._client import SpawnteeClient, SpawnteeClientError
from ._metrics import PhalaMetrics

logger = get_logger()

# Spawntee task status → orchestrator BuilderStatus mapping
_STATUS_MAP = {
    "pending": ModelRun.BuilderStatus.BUILDING,
    "running": ModelRun.BuilderStatus.BUILDING,
    "completed": ModelRun.BuilderStatus.SUCCESS,
    "failed": ModelRun.BuilderStatus.FAILED,
}


class PhalaModelBuilder(Builder):
    """
    Builder that calls the Phala spawntee /build-model API.

    The spawntee handles the full build lifecycle:
    1. Download encrypted model from S3
    2. Decrypt using TEE-derived key
    3. Build a custom Docker image with the model embedded

    The orchestrator submits the build and then polls for completion.
    """

    def __init__(self, config: PhalaRunnerInfrastructureConfig, metrics: PhalaMetrics | None = None):
        super().__init__()
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

    def build(self, model: ModelRun, crunch: Crunch) -> tuple[str, str, str]:
        """
        Submit a build request to the spawntee.

        Args:
            model: ModelRun with code_submission_id identifying the encrypted model on S3.
            crunch: Crunch configuration (unused — spawntee has its own S3 config).

        Returns:
            Tuple of (task_id, docker_image_placeholder, logs_arn).
            docker_image is a placeholder — the real image lives inside the TEE.
        """
        submission_id = model.code_submission_id
        logger.info("Submitting build to spawntee for submission_id=%s", submission_id)

        try:
            result = self._client.build_model(submission_id, model_name=model.name)
        except SpawnteeClientError as e:
            logger.error("Failed to submit build for %s: %s", submission_id, e)
            raise

        task_id = result["task_id"]
        logger.info("Build submitted: task_id=%s for submission_id=%s", task_id, submission_id)

        # docker_image is a placeholder; the actual image is inside the TEE.
        # The task_id is used later by start-model to reference the built image.
        docker_image = f"phala-tee://{submission_id}"
        logs_arn = None  # Logs live inside the TEE, not in CloudWatch
        return task_id, docker_image, logs_arn

    def is_built(self, model: ModelRun, crunch: Crunch) -> tuple[bool, str]:
        """
        Check whether a model image already exists on the spawntee.

        Uses the /submission-image endpoint. If the spawntee is unreachable
        we return False (needs rebuild).
        """
        submission_id = model.code_submission_id

        try:
            result = self._client.check_model_image(submission_id)
            exists = result.get("image_exists", False)
            image_name = result.get("image_name", "")
            task_id = result.get("task_id", "")
            logger.debug("Image check for %s: exists=%s", submission_id, exists)

            if exists and task_id:
                # Set builder_job_id so run() can use it to call start-model
                model.update_builder_status(task_id, ModelRun.BuilderStatus.SUCCESS)

            return exists, f"phala-tee://{image_name}" if exists else ""
        except SpawnteeClientError as e:
            logger.warning("Could not check image for %s: %s", submission_id, e)
            return False, ""

    def load_status(self, model: ModelRun) -> ModelRun.BuilderStatus:
        """Poll the spawntee for a single model's build status."""
        return self.load_statuses([model])[model]

    def load_statuses(self, models: list[ModelRun]) -> dict[ModelRun, ModelRun.BuilderStatus]:
        """
        Poll the spawntee for build statuses of the given models.

        Each model's builder_job_id is the spawntee task_id from build().
        """
        result = {}
        for model in models:
            task_id = model.builder_job_id
            if not task_id:
                result[model] = ModelRun.BuilderStatus.FAILED
                continue

            try:
                task = self._client.get_task(task_id)
                spawntee_status = task.get("status", "failed")
                result[model] = _STATUS_MAP.get(spawntee_status, ModelRun.BuilderStatus.FAILED)

                # Record metrics for completed/failed operations
                if self._metrics and spawntee_status in ("completed", "failed"):
                    self._metrics.record_from_task(task)
            except SpawnteeClientError as e:
                logger.warning("Failed to poll build status for task %s: %s", task_id, e)
                # Don't mark as FAILED on transient network errors — keep current status
                result[model] = model.builder_status or ModelRun.BuilderStatus.BUILDING

        return result
