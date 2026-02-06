from collections import defaultdict
from enum import Enum
from typing import Any, TypedDict

import boto3
from model_orchestrator.entities import Crunch, Infrastructure, ModelRun
from model_orchestrator.services import Runner
from model_orchestrator.utils.compat import batched
from model_orchestrator.utils.logging_utils import get_logger

RPC_PORT = 50051

ASSIGN_PUBLIC_IP_DEFAULT = True
ASSIGN_PUBLIC_IP_KEY = 'assign-public-ip'

_TaskArn = str
_ENIArn = str


class _TaskStatus(TypedDict):
    status: str
    private_ip: str | None
    public_ip: str | None


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
    }

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
                {'name': 'CRUNCH_ONCHAIN_ADDRESS', 'value': crunch.onchain_address},
                {'name': 'CRUNCHER_WALLET_PUBKEY', 'value': model.cruncher_onchain_info.wallet_pubkey},
                {'name': 'CRUNCHER_HOTKEY', 'value': model.cruncher_onchain_info.hotkey},
                {'name': 'COORDINATOR_WALLET_PUBKEY', 'value': crunch.coordinator_info.wallet_pubkey},
                {'name': 'COORDINATOR_CERT_HASH', 'value': crunch.coordinator_info.cert_hash},
                {'name': 'COORDINATOR_CERT_HASH_SECONDARY', 'value': crunch.coordinator_info.cert_hash_secondary},
            ] + ([
                {'name': 'GRPC_TRACE', 'value': 'handshaker, security, tsi'},
                {'name': 'GRPC_VERBOSITY', 'value': 'DEBUG'}
            ] if crunch.infrastructure.debug_grpc else [])
        )

        assign_public_ip = crunch.network_config.get(ASSIGN_PUBLIC_IP_KEY) if crunch.network_config else ASSIGN_PUBLIC_IP_DEFAULT
        if assign_public_ip is None:
            assign_public_ip = True

        task_arn = aws_ecs.run_task(
            cluster_name=cluster_name,
            task_definition=task_definition_arn,
            subnets=subnets,
            security_groups=security_groups,
            job_type=job_type,
            instance_type=(crunch.infrastructure.gpu_config.instances_types[0] if is_gpu else None),  # type: ignore
            assign_public_ip=assign_public_ip,
            tag_name=crunch.name
        )

        logs_arn = f'{logs_prefix_arn}/{task_arn.split("/")[-1]}'

        infos = {
            'cluster_name': cluster_name,
            'assign_public_ip': assign_public_ip,
        }

        return task_arn, logs_arn, infos

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
            grouped_models[cluster_name].append(model.runner_job_id)

        tasks_statuses = {}
        for cluster_name, job_ids in grouped_models.items():
            cluster_tasks_status = aws_ecs.get_tasks_status(cluster_name, job_ids)
            tasks_statuses.update(cluster_tasks_status)

        models_statuses = {}
        for model in models:
            task_state = tasks_statuses.get(model.runner_job_id, None)
            if task_state is None:
                get_logger().warning("AWS ECS: Task not found for model jobid: %s", model.runner_job_id)
                model_runner_status = None
                ip = None
            else:
                model_runner_status = self.ECS_TO_MODEL_RUNNER_STATUS.get(task_state["status"], None)

                if model_runner_status is None:
                    get_logger().error("AWS ECS: Unknown state for model jobid: %s, state: %s", model.runner_job_id, task_state)

                use_public_ip = model.runner_info.get('assign_public_ip', ASSIGN_PUBLIC_IP_DEFAULT)
                ip = task_state["public_ip"] if use_public_ip else task_state["private_ip"]

            runner_status = model_runner_status if model_runner_status else ModelRun.RunnerStatus.FAILED
            models_statuses[model] = runner_status, ip, RPC_PORT

        return models_statuses

    def load_status(self, model: ModelRun) -> tuple[ModelRun.RunnerStatus, str, int]:
        return self.load_statuses([model])[model]

    def stop(self, model: ModelRun) -> str:
        aws_ecs = AwsEcsRunner(None)
        return aws_ecs.stop_task(model.runner_info['cluster_name'], model.runner_job_id)


class AwsEcsRunner:
    class JobType(Enum):
        CPU = "CPU"
        GPU = "GPU"

    def __init__(self, region):
        self.region = region
        self.ecs_client = boto3.client('ecs', region_name=region)

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

    def run_task(
        self,
        *,
        cluster_name: str,
        task_definition: str,
        subnets: list[str],
        security_groups: list[str],
        job_type: JobType,
        instance_type: str | None,
        assign_public_ip: bool,
        tag_name: str
    ):
        """Run a task in ECS."""

        run_task_args = {
            "cluster": cluster_name,
            "launchType": 'FARGATE' if job_type == self.JobType.CPU else 'EC2',
            "networkConfiguration": {
                'awsvpcConfiguration': {
                    'subnets': subnets,
                    'securityGroups': security_groups,
                    'assignPublicIp': 'ENABLED' if assign_public_ip else 'DISABLED',
                }
            },
            "taskDefinition": task_definition,
            "overrides": {
                'containerOverrides': []
            },
            "tags": [
                {"key": "com.crunchdao.competition.name", "value": tag_name}  # Include the competition name
            ]

        }

        if job_type == self.JobType.GPU:
            run_task_args["placementConstraints"] = [
                {
                    'type': 'memberOf',
                    "expression": f"attribute:ecs.instance-type == {instance_type}"
                }
            ]

        response = self.ecs_client.run_task(**run_task_args)
        task_arn = response['tasks'][0]['taskArn']
        get_logger().debug(f"Started Task: {task_arn}")
        return task_arn

    def stop_task(self, cluster_name, task_arn):
        """Stop a running task in ECS."""
        response = self.ecs_client.stop_task(
            cluster=cluster_name,
            task=task_arn,
            reason="Stopping task requested"
        )
        get_logger().debug(f"Stopped Task: {task_arn}")
        return response

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

            tasks_status_and_ips[task_arn] = {
                "status": task_status,
                "private_ip": None,
                "public_ip": None,
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