import os
from pathlib import Path
from typing import Optional

import boto3
from botocore.exceptions import ClientError

from model_orchestrator.configuration.properties._infrastructure import \
    AwsRunnerInfrastructureConfig

from ...infrastructure import dockerfile
from ...entities import Crunch, Infrastructure, ModelRun
from ...infrastructure.aws.session_manager import SecretManager
from ...services import Builder
from ...utils.compat import batched
from ...utils.logging_utils import get_logger

CURRENT_FILE_DIRECTORY_PATH = Path(__file__).parent


class AwsCodeBuildModelBuilder(Builder):
    CODEBUILD_TO_MODEL_BUILD_STATUS = {
        'IN_PROGRESS': ModelRun.BuilderStatus.BUILDING,
        'SUCCEEDED': ModelRun.BuilderStatus.SUCCESS,
        'FAILED': ModelRun.BuilderStatus.FAILED,
        'FAULT': ModelRun.BuilderStatus.FAILED,
        'TIMED_OUT': ModelRun.BuilderStatus.FAILED,
        'STOPPED': ModelRun.BuilderStatus.FAILED,
    }

    def __init__(self, aws_config: AwsRunnerInfrastructureConfig):
        super().__init__()

        self._aws_config = aws_config

    def _new_builder(self, crunch: Optional[Crunch] = None):
        return AwsCodeBuild(
            region=crunch.infrastructure.zone if crunch else None,
            project_name=self._aws_config.codebuild_project_name,
            s3_bucket_name=self._aws_config.s3_bucket_name,
            ecr_repository_name=self._aws_config.ecr_repository_name,
        )

    def get_ecr_container_uri(self, region, ecr_repository_name):
        return f'{AwsCodeBuild.AWS_ACCOUNT_ID}.dkr.ecr.{region}.amazonaws.com/{ecr_repository_name}'

    def create(self, crunch: Crunch) -> dict:
        builder = self._new_builder(crunch)
        project, logs_prefix_arn = builder.create_project()

        return {
            'project_arn': project['arn'],
            'logs_prefix_arn': logs_prefix_arn,
            'project_name': project['name'],
            'ecr_image_uri': self.get_ecr_container_uri(crunch.infrastructure.zone, builder.ecr_repository_name),
        }

    def build(self, model: ModelRun, crunch: Crunch) -> tuple[str, str, str]:
        builder = self._new_builder(crunch)
        build_id = builder.start_build(
            submission_id=model.code_submission_id,
            resources_id=model.resource_id,
            model_id=model.model_id,
            hardware_type=model.hardware_type,
            docker_tag=model.docker_tag()
        )

        logs_arn = crunch.builder_config['logs_prefix_arn'] + f'/{build_id.split(":")[-1]}'
        return build_id, f'{builder.ecr_repository_name}:{model.docker_tag()}', logs_arn

    def load_statuses(self, models: list[ModelRun]) -> dict[ModelRun, ModelRun.BuilderStatus]:
        builder = self._new_builder()

        build_ids = [model.builder_job_id for model in models]
        if not build_ids:
            return {}

        build_statuses = builder.get_build_status(build_ids)

        model_statuses = {}
        for model in models:
            build_state = build_statuses.get(model.builder_job_id, None)
            if build_state is None:
                get_logger().warning("AWS CodeBuild: Build not found for model jobid: %s", model.builder_job_id)

            model_builder_status = self.CODEBUILD_TO_MODEL_BUILD_STATUS.get(build_state, None)
            if model_builder_status is None:
                get_logger().error("AWS CodeBuild: Unknown state for model jobid: %s, state: %s", model.builder_job_id, build_state)

            model_statuses[model] = self.CODEBUILD_TO_MODEL_BUILD_STATUS.get(build_state, ModelRun.BuilderStatus.FAILED)
        return model_statuses

    def is_built(self, model: ModelRun, crunch: Crunch) -> tuple[bool, str]:
        builder = self._new_builder(crunch)
        exists = builder.docker_image_exists(model.docker_tag())

        return exists, f'{builder.ecr_repository_name}:{model.docker_tag()}' if exists else None


class AwsCodeBuild:
    ROLE_ARN = "arn:aws:iam::728958649654:role/CodeBuildMinimalRole"
    AWS_ACCOUNT_ID = "728958649654"
    _buildspec_content = None

    def __init__(
        self,
        region: Optional[str],
        project_name: str,
        s3_bucket_name: str,
        ecr_repository_name: str
    ):
        self.region = region
        self.project_name = project_name
        self.s3_bucket_name = s3_bucket_name
        self.ecr_repository_name = ecr_repository_name

        self.codebuild_client = boto3.client("codebuild", region_name=region)
        self.account_id = boto3.client("sts").get_caller_identity()["Account"]
        self.docker_credentials = SecretManager(self.region).get_docker_credentials()
        if not self.docker_credentials:
            raise Exception("Docker credentials not found")

    def create_project(self):
        log_group = 'codebuild-logs'
        stream_name = self.project_name
        logs_arn_prefix = f'arn:aws:ecs:{self.region}:{self.account_id}:log-group:{log_group}:log-stream:{stream_name}'

        try:
            response = self.codebuild_client.batch_get_projects(names=[self.project_name])
            if response['projects']:
                project_info = response['projects'][0]
                cloud_watch_logs = project_info['logsConfig']['cloudWatchLogs']
                same_logs = cloud_watch_logs.get('groupName', '') == log_group and \
                            cloud_watch_logs.get('streamName', '') == stream_name and \
                            cloud_watch_logs.get('status', '') == 'ENABLED'

                if same_logs:
                    get_logger().debug(f"Project '{self.project_name}' already exists with the same log configuration.")
                    return project_info, logs_arn_prefix
                else:
                    get_logger().debug(f"Project '{self.project_name}' already exists, but log configuration differs.")
            else:
                get_logger().debug(f"Project '{self.project_name}' does not exist. Proceeding with creation...")

        except ClientError as e:
            get_logger().debug(f"Error checking project: {e}")
            return None, None

        response = self.codebuild_client.create_project(
            name=self.project_name,
            source={
                "type": "NO_SOURCE",
                "buildspec": """
                    version: 0.2

                    phases:
                      build:
                        commands:
                          - echo "Default buildspec. Replace during start_build."
                    artifacts:
                      files: []
                """
            },
            artifacts={
                "type": "NO_ARTIFACTS"
            },
            environment={
                "type": "LINUX_CONTAINER",
                "image": "aws/codebuild/standard:6.0",
                "computeType": "BUILD_GENERAL1_LARGE",
                "privilegedMode": True,  # permit docker build
                "environmentVariables": [
                    {
                        "name": "AWS_DEFAULT_REGION",
                        "value": self.region
                    },
                    {
                        "name": "AWS_ACCOUNT_ID",
                        "value": self.AWS_ACCOUNT_ID
                    },
                    {
                        "name": "IMAGE_REPO_NAME",
                        "value": self.ecr_repository_name
                    }
                ],
            },
            serviceRole=self.ROLE_ARN,
            logsConfig={
                "cloudWatchLogs": {
                    "status": "ENABLED",
                    "groupName": log_group,
                    "streamName": stream_name,
                }
            }
        )

        get_logger().debug(f"Project created: {response['project']['name']}, logs ARN prefix: {logs_arn_prefix}")
        return response['project'], logs_arn_prefix

    def start_build(self, submission_id, resources_id, model_id, hardware_type, docker_tag):
        if not bool(submission_id) or submission_id.strip() == "":
            raise ValueError(f"Invalid submission_id: '{submission_id}'.")

        if not bool(resources_id) or resources_id.strip() == "":
            resources_id = ""

        docker_username, docker_token = self.docker_credentials
        response = self.codebuild_client.start_build(
            projectName=self.project_name,
            sourceVersion="",
            environmentVariablesOverride=[
                {"name": "MODEL_ID", "value": str(model_id), "type": "PLAINTEXT"},
                {"name": "SUBMISSION_ID", "value": str(submission_id), "type": "PLAINTEXT"},  # can't be empty, risk of downloading all codes
                {"name": "RESOURCES_ID", "value": str(resources_id).strip(), "type": "PLAINTEXT"},  # can't be empty, risk of downloading all models, test is in the buildspec.yml
                {"name": "HARDWARE_TYPE", "value": hardware_type.value, "type": "PLAINTEXT"},
                {"name": "IMAGE_TAG", "value": docker_tag, "type": "PLAINTEXT"},
                {"name": "DOCKER_TOKEN", "value": docker_token, "type": "PLAINTEXT"},
                {"name": "DOCKER_USERNAME", "value": docker_username, "type": "PLAINTEXT"},
                {"name": "DOCKERFILE_CONTENT", "value": dockerfile, "type": "PLAINTEXT"},
                {"name": "S3_BUCKET_NAME", "value": self.s3_bucket_name, "type": "PLAINTEXT"},
                {"name": "IMAGE_REPO_NAME", "value": self.ecr_repository_name, "type": "PLAINTEXT"},
            ],
            buildspecOverride=self.get_buildspec_content()
        )
        build_id = response['build']['arn']
        get_logger().debug(f"Build started with ID: {build_id} Waiting for build to complete...")
        return build_id

    def get_build_status(self, build_ids) -> dict[str, str]:
        chunk_size = 100
        aggregated_statuses = {}

        # Split tasks_arn into chunks of 100
        for chunk in batched(build_ids, chunk_size):
            chunk = list(chunk)
            chunk_statuses = self._get_build_status(chunk)
            aggregated_statuses.update(chunk_statuses)

        return aggregated_statuses

    def _get_build_status(self, build_ids) -> dict[str, str]:
        response = self.codebuild_client.batch_get_builds(ids=build_ids)
        build_statuses = {build['arn']: build['buildStatus'] for build in response['builds']}
        return build_statuses

    @staticmethod
    def get_buildspec_content():
        if AwsCodeBuild._buildspec_content is None:
            with open(os.path.join(CURRENT_FILE_DIRECTORY_PATH, "buildspec.yml"), 'r') as f:
                buildspec_content = f.read()

        return buildspec_content

    def docker_image_exists(self, image_tag: str) -> bool:
        try:
            ecr_client = boto3.client("ecr", region_name=self.region)
            response = ecr_client.batch_get_image(
                repositoryName=self.ecr_repository_name,
                imageIds=[{"imageTag": image_tag}]
            )
            return len(response.get("images", [])) > 0
        except ClientError as e:
            get_logger().error(f"Failed to check ECR for tag '{image_tag}': {e}")
            return False