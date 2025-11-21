import os
from typing import List, Optional

from pydantic import Field, ValidationError

from ._base import BaseConfig as _BaseConfig
from ._crunch import CpuConfig as CpuConfig
from ._crunch import CrunchConfig
from ._crunch import GpuConfig as GpuConfig
from ._crunch import InfrastructureConfig as InfrastructureConfig
from ._crunch import RunScheduleConfig
from ._infrastructure import InfrastructureConfig
from ._infrastructure import RebuildModeStringType as RebuildModeStringType
from ._logging import LoggingConfig
from ._watcher import WatcherConfig


class SignatureVerifierConfig(_BaseConfig):
    public_key: str = Field(..., description="Public key for signature verification")


class AppConfig(_BaseConfig):
    logging: LoggingConfig = Field(...)
    infrastructure: InfrastructureConfig = Field(...)
    watcher: WatcherConfig = Field(...)
    crunches: List[CrunchConfig] = Field(..., description="List of crunch configurations")
    signature_verifier: Optional[SignatureVerifierConfig] = Field(None)
    tournament_api_url: str = Field(default="https://api.hub.crunchdao.com/", description="URL of the tournament API")
    use_augmented_info: bool = Field(True, description="Enable fetching of augmented information for models from the tournament API.")
    can_place_in_quarantine: bool = Field(True, description="Whether the orchestrator can place models in quarantine when multiple errors occur")

    @staticmethod
    def from_yaml(yaml_content: str, *, exit_on_failure=True) -> 'AppConfig':
        import yaml
        expanded = os.path.expandvars(yaml_content)
        data = yaml.safe_load(expanded)

        try:
            return AppConfig(**data)
        except ValidationError as validation_error:
            if not exit_on_failure:
                raise

            for error in validation_error.errors():
                print(error["type"], error["loc"], error["msg"])

            exit(1)

    @staticmethod
    def from_file(file_path: str, *, exit_on_failure=True) -> 'AppConfig':
        with open(file_path, 'r') as f:
            return AppConfig.from_yaml(f.read(), exit_on_failure=exit_on_failure)
