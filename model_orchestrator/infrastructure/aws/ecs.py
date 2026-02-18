from collections import defaultdict
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, TypedDict

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from model_orchestrator.entities import Crunch, Infrastructure, ModelRun
from model_orchestrator.services import Runner
from model_orchestrator.utils.compat import batched
from model_orchestrator.utils.logging_utils import get_logger

RPC_PORT = 50051

ASSIGN_PUBLIC_IP_DEFAULT = True
ASSIGN_PUBLIC_IP_KEY = 'assign-public-ip'

_TaskArn = str
_ServiceArn = str
_ServiceName = str
_ENIArn = str


class _TaskStatus(TypedDict):
    status: str
    private_ip: str | None
    public_ip: str | None
    task_arn: str | None
    task_created_at: datetime | None
    service_name: str | None


class AwsEcsModelRunner(Runner):
    ECS_TO_MODEL_RUNNER_STATUS = {
        "PROVISIONING": ModelRun.RunnerStatus.INITIALIZING,
        "PENDING": ModelRun.RunnerStatus.INITIALIZING,
        "ACTIVATING": ModelRun.RunnerStatus.INITIALIZING,
        "RUNNING": ModelRun.RunnerStatus.RUNNING,
        "DEACTIVATING": ModelRun.RunnerStatus.STOPPING,
        "STOPPING": ModelRun.RunnerStatus.STOPPING,
        "DEPROVISIONING": ModelRun.RunnerStatus.STOPPING,
        "STOPPED": ModelRun.RunnerStatus.STOPPED,
        "DELETED": ModelRun.RunnerStatus.STOPPED,
        "FAILED": ModelRun.RunnerStatus.FAILED,
        "RECOVERING": ModelRun.RunnerStatus.RECOVERING,
    }

    def __init__(self, config):
        self.max_task_restarts = config.max_task_restarts
        self.restart_window_hours = config.restart_window_hours

    def run(self, model: ModelRun, crunch: Crunch) -> tuple[str, str, Any]:
        aws_ecs = AwsEcsRunner(crunch.infrastructure.zone)

        if crunch.infrastructure.runner_type != Infrastructure.RunnerType.AWS_ECS:
            raise ValueError(f"Invalid runner type: {crunch.infrastructure.runner_type}. Expected '{Infrastructure.RunnerType.AWS_EC2}'.")

        job_type = AwsEcsRunner.JobType(model.hardware_type.value)

        cluster_name = crunch.runner_config['cluster_name']
        task_execution_role_arn = crunch.runner_config['task_execution_role_arn']
        subnets = crunch.network_config['subnets']
        security_groups = crunch.network_config['security_groups']

        image_uri = f'{crunch.builder_config["ecr_image_uri"]}:{model.docker_image.split(":")[-1]}'

        infrastructure_config = crunch.infrastructure

        is_gpu = job_type == AwsEcsRunner.JobType.GPU
        task_definition_arn, logs_prefix_arn = aws_ecs.register_task_definition(
            family_name=cluster_name,
            container_name=model.docker_image,
            docker_image=image_uri,
            job_type=job_type,
            vcpus=infrastructure_config.gpu_config.vcpus if is_gpu else infrastructure_config.cpu_config.vcpus,
            memory=infrastructure_config.gpu_config.memory if is_gpu else infrastructure_config.cpu_config.memory,
            gpu_count=infrastructure_config.gpu_config.gpus if is_gpu else None,
            execution_role_arn=task_execution_role_arn,
            env=[
                    {'name': 'SECURE', 'value': str(crunch.infrastructure.is_secure)},
                    {'name': 'MODEL_ID', 'value': model.model_id},
                    {'name': 'CRUNCH_ONCHAIN_ADDRESS', 'value': crunch.onchain_address or ''},
                    {'name': 'CRUNCHER_WALLET_PUBKEY', 'value': model.cruncher_onchain_info.wallet_pubkey or ''},
                    {'name': 'CRUNCHER_HOTKEY', 'value': model.cruncher_onchain_info.hotkey or ''},
                    {'name': 'COORDINATOR_WALLET_PUBKEY', 'value': (crunch.coordinator_info.wallet_pubkey if crunch.coordinator_info else '') or ''},
                    {'name': 'COORDINATOR_CERT_HASH', 'value': (crunch.coordinator_info.cert_hash if crunch.coordinator_info else '') or ''},
                    {'name': 'COORDINATOR_CERT_HASH_SECONDARY', 'value': (crunch.coordinator_info.cert_hash_secondary if crunch.coordinator_info else '') or ''},
                ] + ([
                         {'name': 'GRPC_TRACE', 'value': 'handshaker, security, tsi'},
                         {'name': 'GRPC_VERBOSITY', 'value': 'DEBUG'}
                     ] if crunch.infrastructure.debug_grpc else [])
        )

        assign_public_ip = crunch.network_config.get(ASSIGN_PUBLIC_IP_KEY) if crunch.network_config else ASSIGN_PUBLIC_IP_DEFAULT
        if assign_public_ip is None:
            assign_public_ip = True

        service_name = aws_ecs.create_service(
            cluster_name=cluster_name,
            service_name=f'{crunch.name}--{model.model_id}--{model.code_submission_id}',
            task_definition=task_definition_arn,
            subnets=subnets,
            security_groups=security_groups,
            job_type=job_type,
            instance_type=(crunch.infrastructure.gpu_config.instances_types[0] if is_gpu else None),  # type: ignore
            assign_public_ip=assign_public_ip,
            tags={
                'com.crunchdao.competition.name': crunch.name,
                'com.crunchdao.model.name': model.augmented_info.name if model.augmented_info else '',
                'com.crunchdao.model.id': model.model_id,
                'com.crunchdao.cruncher.name': model.augmented_info.cruncher_name if model.augmented_info else '',
            }
        )

        infos = {
            'cluster_name': cluster_name,
            'assign_public_ip': assign_public_ip,
            'service_name': service_name,
        }

        return service_name, logs_prefix_arn, infos

    def create(self, crunch: Crunch) -> dict:
        aws_ecs = AwsEcsRunner(crunch.infrastructure.zone)
        cluster_name = crunch.infrastructure.cluster_name
        if not cluster_name:
            cluster_name = crunch.id
            get_logger().warning(f"Cluster name not configured. Using crunch ID as cluster name: {cluster_name}")

        cluster_created_name = aws_ecs.create_cluster(cluster_name)

        return {
            'cluster_name': cluster_created_name,
            'task_execution_role_arn': 'arn:aws:iam::728958649654:role/ecsTaskExecutionRole'
        }

    def load_statuses(
        self,
        models: list[ModelRun],
    ) -> dict[ModelRun, (ModelRun.RunnerStatus, str, int)]:
        # FIXME region is always required, can be ignored if configured in a aws profile
        aws_ecs = AwsEcsRunner(None)

        grouped_models = defaultdict(list)
        for model in models:
            cluster_name = model.runner_info['cluster_name']
            service_name = model.runner_info.get('service_name', model.runner_job_id)
            grouped_models[cluster_name].append(service_name)

        services_statuses = {}
        for cluster_name, service_names in grouped_models.items():
            cluster_services_status = aws_ecs.get_services_status(cluster_name, service_names)
            services_statuses.update(cluster_services_status)

        models_statuses = {}
        for model in models:
            cluster_name = model.runner_info['cluster_name']
            service_name = model.runner_info.get('service_name', model.runner_job_id)
            service_state = services_statuses.get(service_name, None)
            if service_state is None:
                get_logger().warning("AWS ECS: Service not found for model: %s", service_name)
                model_runner_status = None
                ip = None
            else:
                if service_state["status"] == 'PENDING' and model.runner_status in (ModelRun.RunnerStatus.RUNNING, ModelRun.RunnerStatus.RECOVERING):
                    service_state["status"] = 'RECOVERING'

                model_runner_status = self.ECS_TO_MODEL_RUNNER_STATUS.get(service_state["status"], None)

                if model_runner_status is None:
                    get_logger().error("AWS ECS: Unknown state for model service: %s, state: %s", service_name, service_state)

                use_public_ip = model.runner_info.get('assign_public_ip', ASSIGN_PUBLIC_IP_DEFAULT)
                ip = service_state["public_ip"] if use_public_ip else service_state["private_ip"]

                # Track task restarts to detect excessive failures
                # AWS doesn't provide a solution for managing that. A model can restart infinitely if it succeeds in starting at least once.
                if self._check_excessive_restarts(model, service_state.get("task_arn"), service_state.get("task_created_at")):
                    get_logger().debug(f"Model {model.model_id} has excessive restarts ({self.max_task_restarts}+ in {self.restart_window_hours}h), marking as FAILED")
                    model_runner_status = ModelRun.RunnerStatus.FAILED
                    self.stop(model)

            runner_status = model_runner_status if model_runner_status else ModelRun.RunnerStatus.FAILED
            models_statuses[model] = runner_status, ip, RPC_PORT

        return models_statuses

    def _check_excessive_restarts(self, model: ModelRun, current_task_arn: str | None, task_created_at: datetime | None) -> bool:
        """
        Track task ARNs in runner_info and check if restarts exceed threshold within time window.
        Returns True if model should be marked as FAILED due to excessive restarts.
        """
        if not current_task_arn or not task_created_at:
            return False

        task_history = model.runner_info.get('task_history', [])

        # Check if this is a new task
        last_task_arn = task_history[-1]['task_arn'] if task_history else None
        if current_task_arn != last_task_arn:
            task_history.append({
                'task_arn': current_task_arn,
                'created_at': task_created_at.isoformat() if task_created_at else None
            })

        model.runner_info['task_history'] = task_history

        # Filter to tasks within the time window
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(hours=self.restart_window_hours)
        recent_tasks = []
        for t in task_history:
            created_at_str = t['created_at']
            if not created_at_str:
                continue
            # Parse ISO format string back to datetime
            created_at = datetime.fromisoformat(created_at_str)
            # Handle naive datetimes (assume UTC)
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)
            if created_at > cutoff:
                recent_tasks.append(t)

        # Check if exceeded threshold (more than MAX means excessive restarts)
        return len(recent_tasks) > self.max_task_restarts

    def load_status(self, model: ModelRun) -> tuple[ModelRun.RunnerStatus, str, int]:
        return self.load_statuses([model])[model]

    def stop(self, model: ModelRun) -> str:
        aws_ecs = AwsEcsRunner(None)
        service_name = model.runner_info.get('service_name', model.runner_job_id)
        aws_ecs.stop_service(model.runner_info['cluster_name'], service_name)
        return service_name


class AwsEcsRunner:
    class JobType(Enum):
        CPU = "CPU"
        GPU = "GPU"

    def __init__(self, region):
        self.region = region
        # Adaptive retry mode handles throttling with exponential backoff
        retry_config = Config(retries={'max_attempts': 10, 'mode': 'adaptive'})
        self.ecs_client = boto3.client('ecs', region_name=region, config=retry_config)

    def cluster_exists(self, cluster_name):
        """Check if an ECS cluster exists."""
        response = self.ecs_client.describe_clusters(clusters=[cluster_name])
        return any(cluster['clusterName'] == cluster_name for cluster in response.get('clusters', []))

    def create_cluster(self, cluster_name):
        if self.cluster_exists(cluster_name):
            get_logger().debug(f"Cluster '{cluster_name}' already exists.")
        else:
            response = self.ecs_client.create_cluster(clusterName=cluster_name)
            get_logger().debug(f"Created ECS Cluster: {response['cluster']['clusterArn']}")
        return cluster_name

    def get_latest_task_definition(self, family_name):
        """
        Retrieves the latest revision of a task definition in ECS.
        """
        response = self.ecs_client.list_task_definitions(
            familyPrefix=family_name,
            sort='DESC',  # Sort by the most recent revision
            maxResults=1
        )

        if response['taskDefinitionArns']:
            # Fetch and return details of the latest task definition
            return self.ecs_client.describe_task_definition(
                taskDefinition=response['taskDefinitionArns'][0]
            )['taskDefinition']
        return None

    def is_task_definition_updated(self, existing_task_def, new_task_def_config):
        """
        Compares the existing task definition with the new configuration
        to detect changes.
        """
        # Extract fields relevant for comparison
        relevant_fields = ['containerDefinitions', 'cpu', 'memory', 'executionRoleArn', 'networkMode', 'requiresCompatibilities']

        # Compare each field in the relevant fields
        for field in relevant_fields:
            if existing_task_def.get(field) != new_task_def_config.get(field):  # todo improve compare...
                return True  # TODO Changes detected

        return False  # No changes detected

    def register_task_definition(
        self,
        family_name,
        container_name,
        docker_image,
        vcpus: int,
        memory: int,
        job_type: JobType,
        gpu_count=None,
        execution_role_arn=None,
        env=None
    ):
        """
        Registers a new task definition only if there are changes
        compared to the latest existing task definition.
        """

        # Convert vCPUs to ECS CPU units (multiples of 1024)
        cpu_units = int(vcpus * 1024)

        log_group = '/aws/ecs/task'
        stream_prefix = family_name
        container_name = container_name.replace(':', '--')

        new_task_def_config = {
            "containerDefinitions": [
                {
                    'name': container_name,
                    'image': docker_image,
                    'portMappings': [
                        {
                            'containerPort': RPC_PORT,
                            'hostPort': RPC_PORT,
                            'protocol': 'tcp',
                            'name': 'grpc',
                            'appProtocol': 'grpc',
                        },
                    ],
                    'privileged': False,
                    'logConfiguration': {
                        'logDriver': 'awslogs',
                        'options': {
                            'awslogs-group': log_group,
                            'awslogs-region': self.region,
                            'awslogs-stream-prefix': stream_prefix,
                        }
                    },
                    'essential': True,
                    'resourceRequirements': [{'value': str(gpu_count), 'type': 'GPU'}] if job_type == self.JobType.GPU and gpu_count is not None else [],
                    'environment': env or [],
                }
            ],
            "networkMode": 'awsvpc',
            "requiresCompatibilities": ['FARGATE'] if job_type == self.JobType.CPU else ['EC2'],
            "executionRoleArn": execution_role_arn,
            "memory": str(memory),
            "cpu": str(cpu_units),
        }

        family_name = f"{family_name}--{container_name}"
        # Fetch the latest task definition, if it exists
        existing_task_def = self.get_latest_task_definition(family_name)

        # Check if there are any changes in the task configuration
        if existing_task_def and not self.is_task_definition_updated(existing_task_def, new_task_def_config):
            get_logger().debug("No changes detected. Using existing task definition.")
            return existing_task_def['taskDefinitionArn']  # Use the existing ARN

        # Register a new task definition if changes are detected
        response = self.ecs_client.register_task_definition(
            family=family_name,
            containerDefinitions=new_task_def_config['containerDefinitions'],
            cpu=new_task_def_config['cpu'],
            memory=new_task_def_config['memory'],
            executionRoleArn=new_task_def_config['executionRoleArn'],
            networkMode=new_task_def_config['networkMode'],
            requiresCompatibilities=new_task_def_config['requiresCompatibilities']
        )

        logs_prefix_arn = f'arn:aws:ecs:{self.region}:{boto3.client("sts").get_caller_identity()["Account"]}:log-group:{log_group}:log-stream:{stream_prefix}/{container_name}'

        get_logger().debug(f"Registered new Task Definition: {response['taskDefinition']['taskDefinitionArn']}")
        return response['taskDefinition']['taskDefinitionArn'], logs_prefix_arn

    def create_service(
        self,
        *,
        cluster_name: str,
        service_name: str,
        task_definition: str,
        subnets: list[str],
        security_groups: list[str],
        job_type: JobType,
        instance_type: str | None,
        assign_public_ip: bool,
        tags: dict[str, str]
    ) -> _ServiceName:
        """Create an ECS service with desired count of 1. Services auto-restart tasks on failures or AWS maintenance."""

        network_configuration = {
            'awsvpcConfiguration': {
                'subnets': subnets,
                'securityGroups': security_groups,
                'assignPublicIp': 'ENABLED' if assign_public_ip else 'DISABLED',
            }
        }

        deployment_configuration = {
            'maximumPercent': 100,
            'minimumHealthyPercent': 0,
            'deploymentCircuitBreaker': {
                'enable': True,
                'rollback': False,
            },
        }

        placement_constraints = [
            {'type': 'memberOf', 'expression': f"attribute:ecs.instance-type == {instance_type}"}
        ] if job_type == self.JobType.GPU else []

        common_args = {
            "cluster": cluster_name,
            "taskDefinition": task_definition,
            "desiredCount": 1,
            "networkConfiguration": network_configuration,
            "deploymentConfiguration": deployment_configuration,
        }

        if placement_constraints:
            common_args["placementConstraints"] = placement_constraints

        existing_service = self._get_service(cluster_name, service_name)
        service_status = existing_service.get('status') if existing_service else None

        if service_status == 'ACTIVE':
            self.ecs_client.update_service(service=service_name, **common_args)
            get_logger().debug(f"Updated Service: {service_name}")
        else:
            if service_status:
                # Service exists but not ACTIVE (DRAINING/INACTIVE) - wait for it
                get_logger().debug(f"Service '{service_name}' is {service_status}, waiting...")
                waiter = self.ecs_client.get_waiter('services_inactive')
                waiter.wait(cluster=cluster_name, services=[service_name])

            aws_tags = [{'key': k, 'value': v} for k, v in tags.items()]
            self.ecs_client.create_service(
                serviceName=service_name,
                launchType='FARGATE' if job_type == self.JobType.CPU else 'EC2',
                tags=aws_tags,
                enableECSManagedTags=True,
                propagateTags="SERVICE",
                **common_args
            )
            get_logger().debug(f"Created Service: {service_name}")
        return service_name

    def _get_service(self, cluster_name: str, service_name: str) -> dict | None:
        """Get service details if it exists."""
        try:
            response = self.ecs_client.describe_services(
                cluster=cluster_name,
                services=[service_name]
            )
            services = response.get('services', [])
            if services:
                return services[0]
        except ClientError:
            pass
        return None

    def stop_service(self, cluster_name: str, service_name: str):
        """Stop an ECS service by scaling to 0 tasks."""
        try:
            self.ecs_client.update_service(
                cluster=cluster_name,
                service=service_name,
                desiredCount=0
            )
            get_logger().debug(f"Stopped service '{service_name}' (scaled to 0 tasks)")
        except ClientError as e:
            error_code = e.response['Error']['Code']
            if error_code == 'ServiceNotFoundException':
                get_logger().debug(f"Service '{service_name}' not found")
            elif error_code == 'ServiceNotActiveException':
                get_logger().debug(f"Service '{service_name}' already not active")
            else:
                raise

    def delete_service(self, cluster_name: str, service_name: str):
        """Delete an ECS service. First scales to 0, then deletes."""
        try:
            # First, update desired count to 0 to stop all tasks
            try:
                self.ecs_client.update_service(
                    cluster=cluster_name,
                    service=service_name,
                    desiredCount=0
                )
                get_logger().debug(f"Scaled service '{service_name}' to 0 tasks")
            except ClientError as e:
                error_code = e.response['Error']['Code']
                if error_code in ('ServiceNotActiveException', 'ServiceNotFoundException'):
                    get_logger().debug(f"Service '{service_name}' not active/found, skipping scale down")
                else:
                    raise

            # Then delete the service
            response = self.ecs_client.delete_service(
                cluster=cluster_name,
                service=service_name,
                force=True  # Force delete even if there are running tasks
            )
            get_logger().debug(f"Deleted Service: {service_name}")
            return response
        except ClientError as e:
            if e.response['Error']['Code'] == 'ServiceNotFoundException':
                get_logger().warning(f"Service '{service_name}' not found, may already be deleted")
                return None
            raise

    def _describe_services_batched(
        self,
        cluster_name: str,
        service_names: list[_ServiceName],
        chunk_size: int = 10
    ) -> list[dict]:
        """
        Describe services in batches to handle AWS limit of 10 services per call.
        Returns aggregated list of service descriptions.
        """
        all_services = []
        for chunk in batched(service_names, chunk_size):
            try:
                response = self.ecs_client.describe_services(
                    cluster=cluster_name,
                    services=list(chunk)
                )
                all_services.extend(response.get('services', []))
            except ClientError as e:
                get_logger().error(f"Error describing services batch: {e}")
        return all_services

    def get_services_status(
        self,
        cluster_name: str,
        service_names: list[_ServiceName],
    ) -> dict[_ServiceName, _TaskStatus]:
        """
        Get the status of ECS services by checking their running tasks.
        Returns the status of the first running task for each service.
        Only returns entries for services that are actually found.
        """
        services_status: dict[_ServiceName, _TaskStatus] = {}

        services = self._describe_services_batched(cluster_name, service_names)

        for service in services:
            service_name = service['serviceName']
            running_count = service.get('runningCount', 0)
            desired_count = service.get('desiredCount', 0)
            status = service.get('status', 'INACTIVE')

            # Check deployment state for circuit breaker and retries
            deployments = service.get('deployments', [])
            primary_deployment = next((d for d in deployments if d.get('status') == 'PRIMARY'), None)
            rollout_state = primary_deployment.get('rolloutState') if primary_deployment else None
            failed_tasks = primary_deployment.get('failedTasks', 0) if primary_deployment else 0

            # Determine service-level status (not task-level)
            if rollout_state == 'FAILED':
                service_status = "FAILED"
            elif rollout_state == 'IN_PROGRESS' and failed_tasks > 0:
                service_status = "RECOVERING"
            elif status == 'DRAINING':
                service_status = "STOPPING"
            elif status == 'INACTIVE':
                service_status = "STOPPED"
            elif desired_count == 0 and running_count == 0:
                service_status = "STOPPED"
            elif desired_count == 0 and running_count > 0:
                service_status = "STOPPING"
            elif running_count == 0 and desired_count > 0:
                service_status = "PENDING"
            else:
                service_status = "RUNNING"

            services_status[service_name] = {"status": service_status, "private_ip": None, "public_ip": None, "task_arn": None, "task_created_at": None, "service_name": service_name}

        # List all tasks in cluster (1 paginated call instead of N calls per service)
        all_task_arns = self._list_tasks_paginated(cluster_name, desired_status='RUNNING')

        if all_task_arns:
            # Get task details including service_name from group field
            tasks_info = self.get_tasks_status(cluster_name, all_task_arns)

            # Update IPs, task ARN and created_at from task info (keep service-level status)
            for task_arn, task_info in tasks_info.items():
                service_name = task_info.get("service_name")
                if service_name and service_name in services_status:
                    # Only update if we don't have IP info yet (use first task)
                    if services_status[service_name]["task_arn"] is None:
                        services_status[service_name]["private_ip"] = task_info["private_ip"]
                        services_status[service_name]["public_ip"] = task_info["public_ip"]
                        services_status[service_name]["task_arn"] = task_arn
                        services_status[service_name]["task_created_at"] = task_info.get("task_created_at")

        return services_status

    def stop_task(self, cluster_name, task_arn):
        """Stop a running task in ECS. Kept for backwards compatibility."""
        response = self.ecs_client.stop_task(
            cluster=cluster_name,
            task=task_arn,
            reason="Stopping task requested"
        )
        get_logger().debug(f"Stopped Task: {task_arn}")
        return response

    def _list_tasks_paginated(
        self,
        cluster_name: str,
        desired_status: str = 'RUNNING'
    ) -> list[_TaskArn]:
        """
        List all tasks in a cluster with pagination.
        Returns all task ARNs matching the desired status.
        """
        all_task_arns = []
        next_token = None

        while True:
            try:
                kwargs = {
                    'cluster': cluster_name,
                    'desiredStatus': desired_status,
                }
                if next_token:
                    kwargs['nextToken'] = next_token

                response = self.ecs_client.list_tasks(**kwargs)
                all_task_arns.extend(response.get('taskArns', []))

                next_token = response.get('nextToken')
                if not next_token:
                    break
            except ClientError as e:
                get_logger().error(f"Error listing tasks: {e}")
                break

        return all_task_arns

    def get_tasks_status(
        self,
        cluster_name: str,
        tasks_arn: list[_TaskArn],
        chunk_size=100,
    ) -> dict[_TaskArn, _TaskStatus]:
        """
        Splits the task ARNs into chunks of 100 and invokes `_get_tasks_status` on each chunk.
        Aggregates and returns the combined results.
        """

        aggregated_statuses: dict[_TaskArn, _TaskStatus] = {}

        # Split tasks_arn into chunks of 100
        for chunk in batched(tasks_arn, chunk_size):
            chunk_statuses = self._get_tasks_status(cluster_name, list(chunk))
            aggregated_statuses.update(chunk_statuses)

        return aggregated_statuses

    def _get_tasks_status(
        self,
        cluster_name: str,
        tasks_arn: list[_TaskArn]
    ) -> dict[_TaskArn, _TaskStatus]:
        """
         Optimized method to retrieve the status of ECS tasks and their associated public IPs.
         Assumes ENIs are uniquely associated with tasks.
        """
        # Retrieve ECS task details in a single call
        response = self.ecs_client.describe_tasks(cluster=cluster_name, tasks=tasks_arn)

        if not response['tasks']:
            get_logger().debug(f"No tasks found in cluster '{cluster_name}' for ARNs: {tasks_arn}.")
            return {}

        tasks_status_and_ips: dict[_TaskArn, _TaskStatus] = {}
        task_to_eni: dict[_TaskArn, _ENIArn] = {}  # Direct mapping between task ARN and ENI

        # Collect task statuses and their associated ENIs
        for task in response['tasks']:
            task_status: str = task['lastStatus']
            task_arn: _TaskArn = task['taskArn']
            task_created_at: datetime | None = task.get('createdAt')
            # Extract service name from group (format: "service:service-name")
            group = task.get('group', '')
            service_name = group.replace('service:', '') if group.startswith('service:') else None

            tasks_status_and_ips[task_arn] = {
                "status": task_status,
                "private_ip": None,
                "public_ip": None,
                "task_arn": task_arn,
                "task_created_at": task_created_at,
                "service_name": service_name,
            }

            if task_status != "RUNNING":
                continue

            for attachment in task.get("attachments", []):
                for detail in attachment.get("details", []):
                    detail_name = detail.get("name")

                    if detail_name == "privateIPv4Address":
                        private_ip = detail.get("value")
                        tasks_status_and_ips[task_arn]["private_ip"] = private_ip

                    elif detail_name == "networkInterfaceId":
                        eni = detail.get("value")
                        task_to_eni[task_arn] = eni  # Map ENI directly to the task

        if task_to_eni:
            ec2_client = boto3.client("ec2", region_name=self.region)
            eni_details = ec2_client.describe_network_interfaces(NetworkInterfaceIds=list(task_to_eni.values()))

            # Map ENI IDs to public IPs
            eni_to_public_ip = {
                eni["NetworkInterfaceId"]: eni["Association"].get("PublicIp")
                for eni in eni_details.get("NetworkInterfaces", [])
                if "Association" in eni
            }

            for task_arn, eni in task_to_eni.items():
                tasks_status_and_ips[task_arn]["public_ip"] = eni_to_public_ip.get(eni)

        return tasks_status_and_ips
