from typing import List, Optional, Dict, Any

from pydantic import Field, field_validator
from zoneinfo import available_timezones

from ._base import BaseConfig as _BaseConfig


class CpuConfig(_BaseConfig):
    vcpus: int = Field(..., description="Number of virtual CPUs")
    memory: int = Field(..., description="Memory in MB")
    instances_types: Optional[List[str]] = Field(None, description="List of instance types")


class GpuConfig(_BaseConfig):
    vcpus: int = Field(..., description="Number of virtual CPUs")
    memory: int = Field(..., description="Memory in MB")
    instances_types: Optional[List[str]] = Field(None, description="List of instance types")
    gpus: int = Field(..., description="Number of GPUs")


class InfrastructureConfig(_BaseConfig):
    cluster_name: Optional[str] = Field(None, description="AWS cluster name")
    zone: str = Field(..., description="AWS zone or region")
    cpu_config: Optional[CpuConfig] = Field(None, description="CPU configuration")
    gpu_config: Optional[GpuConfig] = Field(None, description="GPU configuration")
    is_secure: bool = Field(False, description="Specifies whether the infrastructure setup uses a security model protocol.")
    debug_grpc: bool = Field(False, description="Enables gRPC debug logging (GRPC_TRACE and GRPC_VERBOSITY).")


class RunScheduleConfig(_BaseConfig):
    intervals: List[str] = Field(..., description="List of time-based schedule intervals (e.g. 'Mo-Fr 09:00-17:00').")
    timezone: str = Field(..., description="Timezone in IANA format (e.g. 'UTC', 'Europe/Paris', 'America/New_York').")

    @field_validator("timezone")
    def validate_timezone(cls, v):
        if v not in available_timezones():
            raise ValueError(f"Invalid timezone: {v}")
        return v


class CrunchConfig(_BaseConfig):
    id: str = Field(..., description="Unique crunch identifier")
    name: str = Field(..., description="Display name for the crunch")
    onchain_name: str = Field(None, description="Crunch name/identifier on-chain")
    infrastructure: Optional[InfrastructureConfig] = Field(None, description="Infrastructure configuration")
    network_config: Optional[Dict[str, Any]] = Field(None, description="Network configuration")
    run_schedule: Optional[RunScheduleConfig] = Field(None, description="Defines the schedule for running the models of this crunch.")


class CrunchesConfig(_BaseConfig):
    crunches: List[CrunchConfig] = Field(..., description="List of crunch configurations")
