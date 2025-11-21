from datetime import datetime

from .errors import ErrorType


class Failure:
    def __init__(self,
                 model_run_id: str,
                 error_code: ErrorType,
                 reason: str,
                 exception: Exception | str | None,
                 traceback: str | None = None,
                 occurred_at: datetime | None = None) -> None:
        self.model_run_id = model_run_id
        self.error_code = error_code
        self.reason = reason
        self.exception = exception
        self.traceback = traceback
        self.occurred_at = occurred_at if occurred_at else datetime.now()
