from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from zoneinfo import ZoneInfo
from opening_hours import OpeningHours

from model_orchestrator.configuration.properties import RunScheduleConfig


class RunnerType(Enum):
    AWS_ECS = "AWS_ECS"
    LOCAL = "LOCAL"


@dataclass(kw_only=True)
class HardwareConfig:
    vcpus: int
    memory: int
    instances_types: list[str] | None = None


@dataclass
class CpuConfig(HardwareConfig):
    pass


@dataclass(kw_only=True)
class GpuConfig(HardwareConfig):
    gpus: int


class ScheduleStatus(Enum):
    IN_SCHEDULE = "IN_SCHEDULE"
    OUT_OF_SCHEDULE = "OUT_OF_SCHEDULE"
    NO_SCHEDULE = "NO_SCHEDULE"


class RunSchedule:
    def __init__(self,
                 intervals: str,
                 timezone: ZoneInfo | None):
        self.intervals = intervals
        self.timezone = timezone
        self._oh = OpeningHours(self.intervals, timezone=self.timezone)

    @staticmethod
    def from_config(config: RunScheduleConfig):
        return RunSchedule("; ".join(config.intervals), ZoneInfo(config.timezone) if config.timezone else None)

    def is_allowed_now(self) -> bool:
        return self._oh.is_open(datetime.now(tz=self.timezone))

    def __str__(self) -> str:
        return f"RunSchedule(intervals={self.intervals}, timezone={self.timezone})"

    def __repr__(self) -> str:
        return self.__str__()


@dataclass
class Infrastructure:
    cluster_name: str | None
    zone: str
    runner_type: RunnerType
    cpu_config: CpuConfig | None = None
    gpu_config: GpuConfig | None = None

    RunnerType = RunnerType
    HardwareConfig = HardwareConfig
    CpuConfig = CpuConfig
    GpuConfig = GpuConfig


@dataclass
class Crunch:
    id: str
    name: str
    onchain_name: str
    infrastructure: Infrastructure
    # dict with values custom to infrastructure solution AWS or other (RunnerType)
    # used to store values when the crunch is set up
    runner_config: dict | None = None
    builder_config: dict | None = None
    network_config: dict | None = None

    run_schedule: RunSchedule | None = None

    def can_model_run_now(self) -> bool:
        if self.run_schedule:
            return self.run_schedule.is_allowed_now()
        return True

    def get_schedule_status(self) -> ScheduleStatus:
        if self.run_schedule:
            return ScheduleStatus.IN_SCHEDULE if self.run_schedule.is_allowed_now() else ScheduleStatus.OUT_OF_SCHEDULE
        return ScheduleStatus.NO_SCHEDULE
