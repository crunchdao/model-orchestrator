"""
Phala TEE Model Builder.

Delegates model building to the remote spawntee service via HTTP.
The spawntee downloads encrypted model files from S3, decrypts them
inside the TEE, and builds a Docker image — all remotely.

Uses PhalaCluster for CVM routing:
- New builds go to the head CVM (with capacity check + auto-provisioning)
- Status polls and image checks route to the correct CVM via task_id
- is_built and check_already_running scan all CVMs
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ...entities import ModelRun
from ...entities.crunch import Crunch
from ...services import Builder
from ...utils.logging_utils import get_logger
from ._client import SpawnteeClientError
from ._metrics import PhalaMetrics

if TYPE_CHECKING:
    from ._cluster import PhalaCluster

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

    def __init__(self, cluster: PhalaCluster, metrics: PhalaMetrics | None = None):
        super().__init__()
        self._cluster = cluster
        self._metrics = metrics

    def create(self, crunch: Crunch) -> dict:
        """No infrastructure to create — the CVM is already running."""
        return {}

    def build(self, model: ModelRun, crunch: Crunch) -> tuple[str, str, str]:
        """
        Submit a build request to the spawntee.

        Checks the head CVM's capacity first. If full, a new runner CVM
        is provisioned automatically before submitting the build.

        Args:
            model: ModelRun with code_submission_id identifying the encrypted model on S3.
            crunch: Crunch configuration (unused — spawntee has its own S3 config).

        Returns:
            Tuple of (task_id, docker_image_placeholder, logs_arn).
            docker_image is a placeholder — the real image lives inside the TEE.
        """
        submission_id = model.code_submission_id
        logger.info("Submitting build to spawntee for submission_id=%s", submission_id)

        # Ensure the head CVM has capacity (provisions new runner if full)
        self._cluster.ensure_capacity()
        client = self._cluster.head_client()

        try:
            result = client.build_model(submission_id, model_name=model.name)
        except SpawnteeClientError as e:
            logger.error("Failed to submit build for %s: %s", submission_id, e)
            raise

        task_id = result["task_id"]

        # Record which CVM owns this task
        self._cluster.register_task(task_id, self._cluster.head_id)

        logger.info("Build submitted: task_id=%s for submission_id=%s on CVM %s",
                     task_id, submission_id, self._cluster.head_id)

        # docker_image is a placeholder; the actual image is inside the TEE.
        # The task_id is used later by start-model to reference the built image.
        docker_image = f"phala-tee://{submission_id}"
        logs_arn = None  # Logs live inside the TEE, not in CloudWatch
        return task_id, docker_image, logs_arn

    def is_built(self, model: ModelRun, crunch: Crunch) -> tuple[bool, str]:
        """
        Check whether a model image already exists on any CVM.

        Scans all CVMs since the model could be on any one of them
        (models are sticky to their CVM).
        """
        submission_id = model.code_submission_id

        for app_id, client in self._cluster.all_clients():
            try:
                result = client.check_model_image(submission_id)
                exists = result.get("image_exists", False)
                image_name = result.get("image_name", "")
                task_id = result.get("task_id", "")

                if exists:
                    logger.debug("Image found for %s on CVM %s", submission_id, app_id)

                    if task_id:
                        # Record which CVM owns this task
                        self._cluster.register_task(task_id, app_id)
                        model.update_builder_status(task_id, ModelRun.BuilderStatus.SUCCESS)

                    return True, f"phala-tee://{image_name}" if image_name else ""
            except SpawnteeClientError as e:
                logger.warning("Could not check image on CVM %s for %s: %s", app_id, submission_id, e)

        return False, ""

    def check_already_running(self, model: ModelRun) -> dict | None:
        """
        Check if a model's container is already running on any CVM.

        Scans all CVMs and matches by submission_id. When a match
        is found, records the task→CVM mapping and returns the model info.
        """
        submission_id = model.code_submission_id

        for app_id, client in self._cluster.all_clients():
            try:
                running = client.get_running_models()
                for rm in running:
                    if rm.get("submission_id") == submission_id:
                        task_id = rm.get("task_id")
                        if task_id:
                            self._cluster.register_task(task_id, app_id)

                        logger.info(
                            "Model %s (submission=%s) already running on CVM %s: task=%s port=%s",
                            model.model_id, submission_id, app_id,
                            rm.get("task_id"), rm.get("external_port"),
                        )
                        return rm
            except SpawnteeClientError as e:
                logger.warning("Could not query running models on CVM %s: %s", app_id, e)

        return None

    def load_status(self, model: ModelRun) -> ModelRun.BuilderStatus:
        """Poll the spawntee for a single model's build status."""
        return self.load_statuses([model])[model]

    def load_statuses(self, models: list[ModelRun]) -> dict[ModelRun, ModelRun.BuilderStatus]:
        """
        Poll the spawntee for build statuses of the given models.

        Routes each poll to the correct CVM via the cluster's task map.
        """
        result = {}
        for model in models:
            task_id = model.builder_job_id
            if not task_id:
                result[model] = ModelRun.BuilderStatus.FAILED
                continue

            client = self._cluster.client_for_task(task_id)
            try:
                task = client.get_task(task_id)
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
