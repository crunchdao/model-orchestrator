from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Dict, Optional

import docker.errors
from docker import DockerClient

from model_orchestrator.entities.crunch import Crunch

from ...entities import ModelRun
from ...services import Runner
from ...utils.logging_utils import get_logger
from ...utils.network import find_free_port
import socket
import time

if TYPE_CHECKING:
    import docker.models.containers

RPC_PORT = '50051/tcp'


logger = get_logger()


class LocalModelRunner(Runner):

    CONTAINER_PREFIX = "crunchdao-model-runner"

    def __init__(
        self,
        docker_client: Optional[DockerClient] = None,
        docker_network_name: Optional[str] = None,
    ):
        super().__init__()

        self._docker_client = docker_client or DockerClient.from_env()

        self._last_log_timestamp_per_container_id: Dict[str, datetime] = {}
        self._docker_network_name = docker_network_name

    def create(self, crunch):
        return {}

    def run(self, model, crunch):
        network_name = self._docker_network_name
        get_logger().debug("Running model %s on local Docker runner with network %s", model.id, network_name)
        container_name = f"{self.CONTAINER_PREFIX}-{model.code_submission_id}"

        try:
            existing_container = self._docker_client.containers.get(container_name)
            get_logger().debug("Container %s already exists, stopping and removing it.", container_name)
            existing_container.stop(timeout=0)
            existing_container.remove()
        except docker.errors.NotFound:
            get_logger().debug("No existing container with name %s found, proceeding to create a new one.", container_name)

        container = self._docker_client.containers.run(
            image=model.docker_image,
            name=container_name,
            detach=True,
            ports={RPC_PORT: find_free_port()},
            # remove=True,
            network=network_name,
        )

        logs_arn = None  # unsupported
        return container.id, logs_arn, {}

    def load_status(self, model):
        if not model.runner_job_id:
            return ModelRun.RunnerStatus.FAILED, '', 0

        try:
            container = self._docker_client.containers.get(model.runner_job_id)
            self._print_logs(model, container)

            state = ModelRun.RunnerStatus.RUNNING if container.status == 'running' else ModelRun.RunnerStatus.STOPPED
            # Determine if coordinator is dockerized based on environment variable
            is_coordinator_dockerized = self._docker_network_name is not None
            
            ip = "localhost"
            port = 0

            if state == ModelRun.RunnerStatus.RUNNING:
                if is_coordinator_dockerized:
                    # For dockerized coordinator, use internal container IP and internal port
                    network_settings = container.attrs.get('NetworkSettings', {})
                    networks = network_settings.get('Networks', {})
                    # Assuming a single network for simplicity, or iterate if multiple
                    network_name = list(networks.keys())[0] if networks else None
                    container_ip = networks.get(network_name, {}).get('IPAddress') if network_name else None
                    internal_grpc_port = int(RPC_PORT.split('/', maxsplit=1)[0]) # The internal gRPC port of the model container

                    if container_ip:
                        ip = container_ip
                        port = internal_grpc_port
                    else:
                        logger.warning(f"Could not determine IP for model {model.id}. Reporting as FAILED.")
                        return ModelRun.RunnerStatus.FAILED, '', 0
                else:
                    # For non-dockerized coordinator, use host-mapped IP and port
                    port_bindings = container.attrs.get('HostConfig', {}).get('PortBindings', {}).get(RPC_PORT, [{}])[0]
                    ip = port_bindings.get('HostIp') or "localhost"
                    port = port_bindings.get('HostPort') or 0

            return state, ip, port
        except docker.errors.NotFound:
            return ModelRun.RunnerStatus.FAILED, '', 0

    def load_statuses(self, models):
        return {
            model: self.load_status(model)
            for model in models
        }

    def stop(self, model):
        container = self._docker_client.containers.get(model.runner_job_id)
        return container.stop(timeout=0)

    def _print_logs(self, model: ModelRun, container: "docker.models.containers.Container"):
        since = self._last_log_timestamp_per_container_id.get(container.id)

        try:
            tail = 100 if since is None else "all"
            logs = container.logs(
                timestamps=True,
                since=since,
                tail=tail,
            ).decode('utf-8').splitlines()

            for log in logs:
                raw_timestamp, log = log.split(' ', 1)
                since = datetime.fromisoformat(raw_timestamp)
                logger.debug(f"Logging %s: %s: %s", model.id, raw_timestamp, log)
        except docker.errors.APIError as error:
            logger.error(f"Error fetching logs for container {container.id}: {error}")

        if since is not None:
            self._last_log_timestamp_per_container_id[container.id] = since + timedelta(seconds=1)