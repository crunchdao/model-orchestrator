from model_orchestrator.entities import ModelRun, ErrorType


class OrchestratorError(Exception):
    def __init__(self,
                 model_run: ModelRun,
                 error_type: ErrorType,
                 original_exception: Exception = None,
                 original_exception_traceback: str = None,
                 reason: str = ""
                 ):
        self.model_run = model_run
        self.error_type = error_type
        self.reason = reason
        self.original_exception = original_exception
        self.original_exception_traceback = original_exception_traceback
        super().__init__(f"{error_type.value}: {reason}")


class OrchestratorErrors(Exception):
    def __init__(self, errors: list[OrchestratorError] = None):
        self.errors = errors
        super().__init__()
