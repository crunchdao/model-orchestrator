from typing import Annotated, Literal, Union

from pydantic import Field, field_validator

from ._base import BaseConfig as _BaseConfig

RebuildModeStringType = Literal["disabled", "from-scratch", "from-checkpoint", "if-code-modified"]


class SqliteDatabaseInfrastructureConfig(_BaseConfig):
    type: Literal["sqlite"] = "sqlite"
    path: str = Field(..., description="Path to the SQLite database.db file")


DatabaseInfrastructureConfig = Union[SqliteDatabaseInfrastructureConfig]


class AwsRunnerInfrastructureConfig(_BaseConfig):
    type: Literal["aws"] = "aws"
    s3_bucket_name: str = Field("crunchdao--competition--staging", description="S3 bucket name for model submissions and resources")
    codebuild_project_name: str = Field("model-builder", description="AWS CodeBuild project name for building models")
    ecr_repository_name: str = Field("crunchers-models-staging", description="AWS ECR name for saving models")


class LocalRunnerInfrastructureConfig(_BaseConfig):
    type: Literal["local"] = "local"
    docker_network_name: str | None = Field(
        default=None,
        description="Docker network name used by docker-compose, by default this is " +
        "the name of the directory where docker-compose.yml is located with " +
        "_default postfix, if set it is assumed the orchestrator is running in " +
        "a Docker container together with all other components, the models, and" +
        "the coordinator",
    )
    submission_storage_path_format: str = Field(..., description="Submission storage path, use {id} for submission ID")
    resource_storage_path_format: str = Field(..., description="Resource storage path, use {id} for resource ID")
    rebuild_mode: RebuildModeStringType = Field("disabled", description="Force rebuild of Docker images, useful for development")


    @field_validator("docker_network_name", mode="before")
    def empty_str_is_none(cls, v):
        if v in ("", "null", "None"):
            return None
        return v

    def format_submission_storage_path(self, submission_id: int | str) -> str:
        return self.submission_storage_path_format.format(id=submission_id)

    def format_resource_storage_path(self, resource_id: int | str) -> str:
        return self.resource_storage_path_format.format(id=resource_id)


class PhalaRunnerInfrastructureConfig(_BaseConfig):
    type: Literal["phala"] = "phala"
    spawntee_port: int = Field(9010, description="Port where the spawntee API is exposed on the CVM")
    request_timeout: int = Field(30, description="HTTP request timeout in seconds for spawntee API calls")
    cluster_name: str = Field("", description="Name prefix for CVM discovery via Phala Cloud API (e.g. 'bird-tracker')")
    phala_api_url: str = Field("https://cloud-api.phala.network", description="Phala Cloud API base URL")
    runner_compose_path: str = Field("", description="Path to docker-compose.phala.runner.yml for auto-provisioning new runner CVMs")
    instance_type: str = Field("tdx.medium", description="Phala CVM instance type for auto-provisioned runners (e.g. tdx.small, tdx.medium, tdx.large)")
    memory_per_model_mb: int = Field(1024, description="Estimated memory per model container in MB. Used to calculate max models per CVM.")
    capacity_threshold: float = Field(0.8, description="Fraction of CVM capacity at which it reports full (0.0-1.0). Passed as CAPACITY_THRESHOLD to provisioned runner CVMs.")
    max_models: int = Field(0, description="Global maximum number of models across the entire cluster. 0 = unlimited.")
    gateway_key_path: str = Field(..., description="Path to the coordinator RSA private key file (PEM) for gateway auth signing.")


RunnerInfrastructureConfig = Union[AwsRunnerInfrastructureConfig, LocalRunnerInfrastructureConfig, PhalaRunnerInfrastructureConfig]


class RabbitMQPublisherInfrastructureConfig(_BaseConfig):
    type: Literal["rabbitmq"] = "rabbitmq"
    url: str = Field(..., description="RabbitMQ server URL")


class WebSocketPublisherInfrastructureConfig(_BaseConfig):
    type: Literal["websocket"] = "websocket"
    address: str = Field(..., description="WebSocket bind address")
    port: int = Field(..., description="WebSocket bind port")
    ping_interval: int | None = Field(
        default=None,
        description="Interval in seconds between WebSocket keep-alive pings. If not set, pings are disabled."
    )
    ping_timeout: int | None = Field(
        default=None,
        description="Maximum time, in seconds, to wait for a pong response to a WebSocket ping before closing the connection. If not set, defaults to the WebSocket library's default."
    )


PublisherInfrastructureConfig = Union[RabbitMQPublisherInfrastructureConfig | WebSocketPublisherInfrastructureConfig]


class InfrastructureConfig(_BaseConfig):
    database: DatabaseInfrastructureConfig = Field(..., discriminator='type')
    runner: RunnerInfrastructureConfig = Field(..., discriminator='type')
    publishers: list[Annotated[PublisherInfrastructureConfig, Field(..., discriminator='type')]]
