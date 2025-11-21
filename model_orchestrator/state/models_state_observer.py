import abc

from model_orchestrator.entities import Failure
from model_orchestrator.entities.model_run import ModelRun


class ModelsStateObserver(abc.ABC):
    """Interface for observing model state changes."""

    @abc.abstractmethod
    def on_runner_state_changed(self, model_run: ModelRun, previous_state: ModelRun.RunnerStatus, new_state: ModelRun.RunnerStatus):
        """Called when a runner state is updated."""
        pass

    @abc.abstractmethod
    def on_build_state_changed(self, model_run: ModelRun, previous_state: ModelRun.BuilderStatus, new_state: ModelRun.BuilderStatus):
        """Called when a build state is updated."""
        pass

    @abc.abstractmethod
    def notify_failure(self, model_run: ModelRun, failure: Failure):
        """Called when a model run fails."""
        pass
