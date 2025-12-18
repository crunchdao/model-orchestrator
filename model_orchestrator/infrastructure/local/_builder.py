import errno
import itertools
import os
import re
import shutil
import tempfile
import threading
from enum import Enum
from typing import Callable, Dict, Optional
from uuid import uuid4

import docker.errors
import requests
from docker import DockerClient

from ...configuration.properties._infrastructure import RebuildModeStringType
from ...infrastructure import dockerfile
from ...entities import ModelRun
from ...services import Builder
from ...utils.logging_utils import get_logger

logger = get_logger()


class RebuildMode(Enum):

    DISABLED = "disabled"
    FROM_SCRATCH = "from-scratch"
    FROM_CHECKPOINT = "from-checkpoint"  # TODO: Need to add a buildarg in the Dockerfile, waiting for the other pull request
    IF_CODE_MODIFIED = "if-code-modified"

    # TODO: Should the enum replace RebuildModeStringType?
    @staticmethod
    def map(mode: RebuildModeStringType) -> "RebuildMode":
        if mode == "disabled":
            return RebuildMode.DISABLED
        elif mode == "from-scratch":
            return RebuildMode.FROM_SCRATCH
        elif mode == "from-checkpoint":
            return RebuildMode.FROM_CHECKPOINT
        elif mode == "if-code-modified":
            return RebuildMode.IF_CODE_MODIFIED
        else:
            raise ValueError(f"Unknown rebuild mode: {mode}")


class LocalModelBuilder(Builder):

    IMAGE_NAME = "crunchdao/model-runner"

    def __init__(
        self,
        submission_storage_path_provider: Callable[[int], str],
        resource_storage_path_provider: Callable[[int], str],
        docker_client: Optional[DockerClient] = None,
        rebuild_mode: RebuildMode = RebuildMode.DISABLED,
    ):
        super().__init__()

        try:
            docker_client = docker_client or DockerClient.from_env()
        except docker.errors.DockerException as error:
            logger.error("Docker client could not be initialized: %s", error)
            raise

        self._builder = _DockerBuild(
            docker_client,
            rebuild_mode,
        )

        self._submission_storage_path_provider = submission_storage_path_provider
        self._resource_storage_path_provider = resource_storage_path_provider

    def create(self, crunch):
        return {}

    def build(self, model, crunch):
        image_name_with_tag = self.to_image_name_with_tag(model)

        logger.info(f"Building model {model.id} with image {image_name_with_tag} and submission path {self._submission_storage_path_provider(model.code_submission_id)} and resource path {self._resource_storage_path_provider(model.resource_id) if model.resource_id else None}")
        build_id = self._builder.start(
            self._submission_storage_path_provider(model.code_submission_id),
            self._resource_storage_path_provider(model.resource_id) if model.resource_id else None,
            image_name_with_tag,
        )

        logs_arn = None  # unsupported
        return build_id, image_name_with_tag, logs_arn

    def is_built(self, model, crunch):
        image_name_with_tag = self.to_image_name_with_tag(model)

        exists = self._builder.exists(image_name_with_tag)

        return exists, image_name_with_tag

    def load_status(self, model):
        return self._builder._get_status(model.builder_job_id)

    def load_statuses(self, models):
        return {
            model: self.load_status(model)
            for model in models
        }

    def to_image_name_with_tag(self, model: ModelRun) -> str:
        return f'{self.IMAGE_NAME}:{model.docker_tag()}'


class _DockerBuild:

    BUILD_LABEL_KEY = 'crunchdao.model-orchestrator.build'

    def __init__(
        self,
        client: DockerClient,
        rebuild_mode: RebuildMode,
    ):
        self._client = client
        self._rebuild_mode = rebuild_mode
        self._build_label_value = uuid4().hex if rebuild_mode != RebuildMode.DISABLED else None

        self._pending_builds: Dict[str, ModelRun.BuilderStatus] = {}
        self._lock = threading.Lock()

    def start(
        self,
        submission_path: str,
        resource_path: Optional[str],
        image_name_with_tag: str
    ) -> str:
        task_name = f"build-{image_name_with_tag}"
        self._report_status(task_name, ModelRun.BuilderStatus.BUILDING)

        def run():
            try:
                self._build(submission_path, resource_path, image_name_with_tag, task_name)
                self._report_status(task_name, ModelRun.BuilderStatus.SUCCESS)
            except Exception as exception:
                logger.error(f"Build failed for {task_name}: {exception}", exc_info=exception)
                self._report_status(task_name, ModelRun.BuilderStatus.FAILED)

        thread = threading.Thread(
            target=run,
            daemon=True,
            name=task_name
        )

        thread.start()

        return task_name

    def _report_status(self, task_name: str, status: ModelRun.BuilderStatus):
        with self._lock:
            self._pending_builds[task_name] = status

    def _get_status(self, task_name: str):
        with self._lock:
            status = self._pending_builds.get(task_name)

            if status is None:
                return ModelRun.BuilderStatus.FAILED

            if status != ModelRun.BuilderStatus.BUILDING:
                del self._pending_builds[task_name]

            return status

    def _build(
        self,
        submission_path: str,
        resource_path: Optional[str],
        image_name_with_tag: str,
        task_name: str
    ):
        with tempfile.TemporaryDirectory(prefix='model-orchestrator-') as temp_dir:
            logger.debug(f"Preparing files to {temp_dir}")

            with open(os.path.join(temp_dir, 'Dockerfile'), 'w') as fd:
                fd.write(dockerfile)

            self._copy(submission_path, os.path.join(temp_dir, 'submission', 'code'))

            # TODO resources not in a nested directory?
            resource_temp_dir = os.path.join(temp_dir, 'resources')

            if resource_path:
                self._copy(resource_path, resource_temp_dir)
            else:
                os.makedirs(resource_temp_dir, exist_ok=True)

            image, _ = self._build_image(temp_dir, image_name_with_tag, task_name)

        return image.id

    def _copy(self, source_path: str, destination_path: str) -> None:
        logger.debug("Copying `%s` to `%s`", source_path, destination_path)
        if not os.path.exists(source_path):
            raise FileNotFoundError(f"{source_path}: does not exist")

        os.makedirs(os.path.dirname(destination_path), exist_ok=True)

        if os.path.isfile(source_path):
            linked = False

            try:
                os.link(source_path, destination_path)
                linked = True
            except FileExistsError:
                linked = True
            except OSError as error:
                # cross-device link, errno is windows only so skip in other oses
                if error.errno != errno.EXDEV:
                    raise

            if not linked:
                shutil.copy2(source_path, destination_path)
        elif os.path.isdir(source_path):
            os.makedirs(destination_path, exist_ok=True)

            for item in os.listdir(source_path):
                source_item = os.path.join(source_path, item)
                destination_item = os.path.join(destination_path, item)

                self._copy(source_item, destination_item)

    # adapted from docker-py
    def _build_image(self, path: str, image_name_with_tag: str, task_name: str):
        from docker.utils.json_stream import json_stream

        resp = self._client.api.build(
            path=path,
            tag=image_name_with_tag,
            rm=True,  # remove successful
            forcerm=True,  # remove unsuccessful
            labels={
                self.BUILD_LABEL_KEY: self._build_label_value
            },
            nocache=self._rebuild_mode == RebuildMode.FROM_SCRATCH,
            buildargs={
                'CRUNCHDAO_BUILD': self._build_label_value,
            } if self._rebuild_mode == RebuildMode.FROM_CHECKPOINT else None,
        )

        if isinstance(resp, str):
            return self._client.images.get(resp), []

        last_event = None
        image_id = None

        log_file_path = os.path.join(tempfile.gettempdir(), f"{task_name}.log")
        os.makedirs(os.path.dirname(log_file_path), exist_ok=True)
        with open(log_file_path, 'w') as log_file:
            result_stream, internal_stream = itertools.tee(json_stream(resp))
            for chunk in internal_stream:
                logger.debug("Building: %s", chunk)
                log_file.write(chunk.get('stream', ''))
                if 'error' in chunk:
                    raise docker.errors.BuildError(chunk['error'], result_stream)

                if 'stream' in chunk:
                    match = re.search(
                        r'(^Successfully built |sha256:)([0-9a-f]+)$',
                        chunk['stream']
                    )

                    if match:
                        image_id = match.group(2)

                last_event = chunk


        if image_id:
            return (self._client.images.get(image_id), result_stream)

        raise docker.errors.BuildError(last_event or 'Unknown', result_stream)

    def exists(self, image_name_with_tag: str) -> bool:
        try:
            image = self._client.images.get(image_name_with_tag)
        except docker.errors.ImageNotFound:
            return False

        if self._build_label_value is None:
            return True

        labels = image.attrs.get('Config', {}).get('Labels') or {}
        return labels.get(self.BUILD_LABEL_KEY) == self._build_label_value