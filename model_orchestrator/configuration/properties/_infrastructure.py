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
    max_task_restarts: int = Field(4, description="Maximum number of task restarts within the restart window before marking as FAILED")
    restart_window_hours: int = Field(24, description="Time window in hours for counting task restarts")


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


RunnerInfrastructureConfig = Union[AwsRunnerInfrastructureConfig, LocalRunnerInfrastructureConfig]


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
