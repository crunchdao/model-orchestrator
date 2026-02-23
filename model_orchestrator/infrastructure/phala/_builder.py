"""
Phala TEE Model Builder.

Delegates model building to the remote spawntee service via HTTP.
The spawntee downloads encrypted model files from S3, decrypts them
inside the TEE, and builds a Docker image — all remotely.

Uses PhalaCluster for CVM routing:
- New builds go to the head CVM (with capacity check + auto-provisioning)
- Status polls and image checks route to the correct CVM via task_id
- is_built scans all CVMs
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ...entities import ModelRun
from ...entities.crunch import Crunch
from ...services import Builder
from ...utils.logging_utils import get_logger
from ._client import SpawnteeAuthenticationError, SpawnteeClientError

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

    def __init__(self, cluster: PhalaCluster):
        super().__init__()
        self._cluster = cluster

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
        Check whether a model image already exists on a CVM **that has capacity**.

        Scans all CVMs (runners first) looking for the image.  If the image
        exists on a CVM that reports ``accepting_new_models=false``, it is
        skipped so the model goes through the normal build path which calls
        ``ensure_capacity`` and routes to a CVM with room.  This prevents
        funnelling all models to the registry after an orchestrator DB reset
        (the registry has stale task history for every model ever built).
        """
        submission_id = model.code_submission_id

        for app_id, client in self._cluster.all_clients():
            try:
                result = client.check_model_image(submission_id)
                if "image_exists" not in result:
                    raise SpawnteeClientError(
                        f"Spawntee /submission-image response missing 'image_exists' field: {result}"
                    )

                if result["image_exists"]:
                    image_name = result.get("image_name")
                    task_id = result.get("task_id")

                    if not image_name or not task_id:
                        raise SpawnteeClientError(
                            f"Spawntee says image exists but response missing "
                            f"'image_name' or 'task_id': {result}"
                        )

                    # Only claim the image if the CVM can accept another model.
                    # Otherwise skip it — the build path will find capacity.
                    if not client.has_capacity():
                        logger.info(
                            "Image found for %s on CVM %s but CVM has no capacity — skipping",
                            submission_id, app_id,
                        )
                        continue

                    logger.debug("Image found for %s on CVM %s", submission_id, app_id)
                    self._cluster.register_task(task_id, app_id)
                    model.update_builder_status(task_id, ModelRun.BuilderStatus.SUCCESS)

                    return True, f"phala-tee://{image_name}"
            except SpawnteeAuthenticationError:
                raise  # critical — do not swallow
            except SpawnteeClientError as e:
                logger.warning("Could not check image on CVM %s for %s: %s", app_id, submission_id, e)

        return False, ""

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
                spawntee_status = task.get("status")
                if spawntee_status is None:
                    raise SpawnteeClientError(
                        f"Spawntee /task response missing 'status' field: {task}"
                    )
                mapped = _STATUS_MAP.get(spawntee_status)
                if mapped is None:
                    raise SpawnteeClientError(
                        f"Spawntee returned unknown build task status: {spawntee_status!r}"
                    )
                result[model] = mapped

            except SpawnteeAuthenticationError:
                raise  # critical — do not swallow
            except SpawnteeClientError as e:
                logger.warning("Failed to poll build status for task %s: %s", task_id, e)
                # Don't mark as FAILED on transient network errors — keep current status
                result[model] = model.builder_status or ModelRun.BuilderStatus.BUILDING

        return result
