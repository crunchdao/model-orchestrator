from typing import Literal, Optional

from pydantic import Field

from ._base import BaseConfig as _BaseConfig


class FileLoggingConfig(_BaseConfig):
    path: str = Field("model-orchestrator.log")

class LoggingConfig(_BaseConfig):
    level: Literal["debug", "info", "warning", "error", "critical", "trace"] = Field("info", description="Logging level")
    file: Optional[FileLoggingConfig] = Field(None)
