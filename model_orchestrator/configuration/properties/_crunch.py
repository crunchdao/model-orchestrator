from typing import List, Optional, Dict, Any

from pydantic import Field, field_validator
from zoneinfo import available_timezones

from ._base import BaseConfig as _BaseConfig


class CpuConfig(_BaseConfig):
    vcpus: float = Field(..., description="Number of virtual CPUs (hard limit for EC2, allocation for Fargate)")
    vcpus_reservation: Optional[float] = Field(None, description="CPU soft limit in vCPUs (used for EC2 placement). If not set, vcpus is used for both placement and hard limit.")
    memory: int = Field(..., description="Memory in MB (hard limit - container killed if exceeded)")
    memory_reservation: Optional[int] = Field(None, description="Memory soft limit in MB (used for EC2 placement, container can burst beyond). If not set, memory is used for both placement and hard limit (Fargate behavior).")
    instances_types: Optional[List[str]] = Field(None, description="List of instance types")


class GpuConfig(_BaseConfig):
    vcpus: float = Field(..., description="Number of virtual CPUs (hard limit for EC2, allocation for Fargate)")
    vcpus_reservation: Optional[float] = Field(None, description="CPU soft limit in vCPUs (used for EC2 placement). If not set, vcpus is used for both placement and hard limit.")
    memory: int = Field(..., description="Memory in MB (hard limit - container killed if exceeded)")
    memory_reservation: Optional[int] = Field(None, description="Memory soft limit in MB (used for EC2 placement, container can burst beyond). If not set, memory is used for both placement and hard limit (Fargate behavior).")
    instances_types: Optional[List[str]] = Field(None, description="List of instance types")
    gpus: int = Field(..., description="Number of GPUs")


class InfrastructureConfig(_BaseConfig):
    cluster_name: Optional[str] = Field(None, description="AWS cluster name")
    zone: str = Field(..., description="AWS zone or region")
    launch_type: str = Field("FARGATE", description="ECS launch type: FARGATE or EC2. EC2 enables soft memory limits (memory_reservation) and bin-packing across shared instances.")
    cpu_config: Optional[CpuConfig] = Field(None, description="CPU configuration")
    gpu_config: Optional[GpuConfig] = Field(None, description="GPU configuration")
    is_secure: bool = Field(False, description="Specifies whether the infrastructure setup uses a security model protocol.")
    runner_envs: Dict[str, str] = Field(default_factory=dict, description="Additional environment variables injected into runner containers. Passed as-is to the ECS task definition (e.g. GRPC_TRACE, GRPC_VERBOSITY, SANDBOX_PROXY_URL).")


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
