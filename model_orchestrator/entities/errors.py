import json
from enum import Enum

NEW_RELIC_ALERT_FLAG = '[New Relic Alert]'


class ErrorType(Enum):
    @property
    def default_reason(self):
        return DEFAULT_REASONS.get(self, "No specific reason provided.")


class OrchestratorErrorType(ErrorType):
    UNKNOWN = "UNKNOWN"
    BUILD_FAILED = "BUILD_FAILED"
    RUN_FAILED = "RUN_FAILED"
    STOP_UNEXPECTED = "STOP_UNEXPECTED"
    IN_QUARANTINE = "IN_QUARANTINE"
    STOP_BEFORE_CLEANUP = "STOP_BEFORE_CLEANUP"


# AWS ECS / AWS CodeBuild
class CloudProviderErrorType(ErrorType):
    RUN_EXCEPTION = "CLOUD_PROVIDER_RUN_EXCEPTION"
    BUILD_EXCEPTION = "CLOUD_PROVIDER_BUILD_EXCEPTION"


class ModelRunnerErrorType(ErrorType):
    CONNECTION_FAILED = "MODEL_RUNNER_CONNECTION_FAILED"
    BAD_IMPLEMENTATION = "MODEL_RUNNER_BAD_IMPLEMENTATION"
    MULTIPLE_TIMEOUT = "MODEL_RUNNER_MULTIPLE_TIMEOUT"
    MULTIPLE_FAILED = "MODEL_RUNNER_MULTIPLE_FAILED"


DEFAULT_REASONS = {
    OrchestratorErrorType.UNKNOWN: "An unknown error has occurred. Please try later, if the problem persists contact support.",
    OrchestratorErrorType.BUILD_FAILED: "The build process failed. Please check the builder logs and fix your code before trying again.",
    OrchestratorErrorType.RUN_FAILED: "The run process failed. Please check the runner logs and fix your code before trying again.",
    OrchestratorErrorType.STOP_UNEXPECTED: "The process stopped unexpectedly. Please try again later, if the problem persists contact support.",
    OrchestratorErrorType.IN_QUARANTINE: "The model is currently in quarantine. This means that it crashed multiple times in a row. You need to update or fix it and resubmit; the same submission cannot be reused.",
    CloudProviderErrorType.RUN_EXCEPTION: "An exception occurred during the cloud provider run. We are informed of the issue and will resolve it as soon as possible.",
    CloudProviderErrorType.BUILD_EXCEPTION: "An exception occurred during the cloud provider build. We are informed of the issue and will resolve it as soon as possible.",
    ModelRunnerErrorType.CONNECTION_FAILED: "Failed to establish a connection to the model runner. We are informed of the issue and will resolve it as soon as possible.",
    ModelRunnerErrorType.BAD_IMPLEMENTATION: "The implementation provided is incorrect or incomplete. Please review and fix your code before trying again.",
    ModelRunnerErrorType.MULTIPLE_TIMEOUT: "The model runner experienced multiple timeouts. Optimize your code before trying again.",
    ModelRunnerErrorType.MULTIPLE_FAILED: "The model runner encountered multiple failures. Inspect runner logs and retry after resolving issues."
}


def serialize_error_type(error: ErrorType) -> str | None:
    if not error:
        return None

    return json.dumps({"type": type(error).__name__, "value": error.value})


def deserialize_error_type(serialized: str):
    if not serialized:
        return None

    try:
        payload = json.loads(serialized)
    except (TypeError, ValueError):
        return None

    enum_name = payload.get("type")
    raw_value = payload.get("value")
    if not enum_name:
        return None

    enum_cls = globals().get(enum_name)
    if not isinstance(enum_cls, type) or not issubclass(enum_cls, ErrorType):
        return None

    try:
        return enum_cls(raw_value)
    except (ValueError, TypeError):
        try:
            return enum_cls[raw_value]
        except Exception:
            return None


def get_model_runner_error(error_type: str):
    try:
        return ModelRunnerErrorType[error_type]
    except KeyError:
        return OrchestratorErrorType.UNKNOWN


if __name__ == "__main__":
    print(OrchestratorErrorType.UNKNOWN.default_reason)
    print(CloudProviderErrorType.RUN_EXCEPTION.default_reason)
    print(ModelRunnerErrorType.MULTIPLE_TIMEOUT.default_reason)
