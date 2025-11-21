import abc
import threading
from typing import Callable

from deepdiff import DeepDiff
from model_orchestrator.entities.model_run import ModelRun
from model_orchestrator.utils.logging_utils import get_logger
import newrelic.agent

class ModelStateConfig:
    """
    Represents a configuration (state) captured by polling.
    """

    def __init__(self, id, name, submission_id, resource_id, hardware_type: str, crunch_id, cruncher_id, desired_state, signature):
        self.id = id
        self.name = name
        self.submission_id = submission_id
        self.resource_id = resource_id
        self.hardware_type = ModelRun.HardwareType(hardware_type) if hardware_type else ModelRun.HardwareType.CPU
        self.crunch_id = crunch_id
        self.cruncher_id = cruncher_id
        self.desired_state = ModelRun.DesiredStatus(desired_state)
        self.signature = signature

    def __str__(self):
        """
        Readable representation of the Config object.
        """
        return (
            f"Config("
            f"id={self.id}, name={self.name}, submission_id={self.submission_id}, "
            f"resource_id={self.resource_id}, hardware_type={self.hardware_type}, "
            f"crunch_id={self.crunch_id}, cruncher_id={self.cruncher_id}, "
            f"desired_state={self.desired_state}, signature={self.signature})"
        )


OnConfigChangeCallback = Callable[[list[ModelStateConfig]], None]


class ModelStateConfigPolling(abc.ABC):

    def __init__(
        self,
        *,
        crunch_names: set[str],
        on_config_change_callback: OnConfigChangeCallback,
        stop_event: threading.Event,
        interval=5
    ):
        """
        Initializes the polling class.
        :param on_config_change_callback: Callback to call when changes are detected
        :param interval: Interval in seconds between each call
        """

        self.crunch_names = crunch_names
        self.on_config_change_callback = on_config_change_callback
        self.interval = interval
        self.previous_state = None
        self.stop_event = stop_event

    @abc.abstractmethod
    def fetch_configs(self) -> list[dict] | None:
        pass

    def fetch_configs_as_state(self):
        return self.create_models_state(self.fetch_configs())

    @abc.abstractmethod
    def create_models_state(self, config_entries) -> list[ModelStateConfig]:
        pass

    def compare_configs(self, old_configs, new_configs):
        """
        Compares the old and new set of configurations.
        """
        if old_configs is None:
            return new_configs
        diff = DeepDiff(old_configs, new_configs, ignore_order=True, view="tree")
        if diff:
            get_logger().trace(f"Config changed: {diff}")
            changes = {}
            for change_type, change_details in diff.items():
                if change_type == "iterable_item_removed":
                    continue
                for change in change_details:
                    while hasattr(change, "up") and change.up and hasattr(change.up, "up") and change.up.up:
                        change = change.up

                    changes[id(change.t2)] = change.t2[0] if isinstance(change.t2, list) else change.t2

            return list(changes.values()) if changes else None
        return None

    def start_polling(self):
        """
        Starts polling.
        Compares configurations and calls the callback if changes are detected.
        """
        while not self.stop_event.is_set():
            self.detect_config_changes()
            self.stop_event.wait(self.interval)  # Pause before the next cycle

    @newrelic.agent.background_task()
    def detect_config_changes(self):
        current_configs = self.fetch_configs()  # Fetch the new state

        if current_configs is not None:
            changes = self.compare_configs(self.previous_state, current_configs)

            if changes:
                changes = self.create_models_state(changes)
                for change in changes:
                    get_logger().trace(f"Changes: {change}")

                self.on_config_change_callback(changes)

            self.previous_state = current_configs

