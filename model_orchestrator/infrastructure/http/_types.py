from enum import Enum
from pydantic import BaseModel

from model_orchestrator.entities import ModelRun


class DesiredState(str, Enum):
    RUNNING = "RUNNING"
    STOPPED = "STOPPED"


class DeploymentStatus(str, Enum):
    RUNNER_INITIALIZING = "RUNNER_INITIALIZING"
    RUNNER_RUNNING = "RUNNER_RUNNING"
    RUNNER_STOPPING = "RUNNER_STOPPING"
    RUNNER_STOPPED = "RUNNER_STOPPED"
    RUNNER_FAILED = "RUNNER_FAILED"
    # Builder
    BUILDER_BUILDING = "BUILDER_BUILDING"
    BUILDER_FAILED = "BUILDER_FAILED"
    BUILDER_SUCCESS = "BUILDER_SUCCESS"

    CREATED = "CREATED"


def make_deployment_status(model_run: "ModelRun") -> DeploymentStatus:
    if model_run.runner_status is not None:
        return DeploymentStatus(f"RUNNER_{model_run.runner_status.value}")

    if model_run.builder_status is not None:
        return DeploymentStatus(f"BUILDER_{model_run.builder_status.value}")

    return DeploymentStatus.CREATED


class LogType(str, Enum):
    builder = "builder"
    runner = "runner"


class ModelResponse(BaseModel):
    id: str
    model_name: str
    crunch_id: str
    desired_state: DesiredState
    cruncher_id: str
    cruncher_name: str


class ModelListItem(BaseModel):
    id: str
    model_name: str | None
    deployment_id: str
    desired_state: DesiredState
    status: DeploymentStatus
    statusMessage: str | None
    crunch_id: str
    cruncher_id: str
    cruncher_name: str | None
    builder_log_uri: str | None
    runner_log_uri: str | None
