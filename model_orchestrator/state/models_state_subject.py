from model_orchestrator.entities.model_run import ModelRun
from model_orchestrator.utils.logging_utils import get_logger


class ModelsStateSubject:
    def __init__(self):
        self.observers = []

    def add_observer(self, observer):
        """Registers a new observer to listen for model state changes."""
        self.observers.append(observer)

    def remove_observer(self, observer):
        """Removes an existing observer."""
        self.observers.remove(observer)

    def notify_runner_state_changed(self, model_run: ModelRun, previous_state: ModelRun.RunnerStatus | None, new_state: ModelRun.RunnerStatus):
        """Notify all observers about a runner state change event."""

        for observer in self.observers:
            try:
                observer.on_runner_state_changed(model_run, previous_state, new_state)
            except Exception as e:
                get_logger().error(f"Error notifying observer {type(observer)} about model run state change of model uid:{model_run.id}, model_id:{model_run.model_id}", exc_info=e)

    def notify_build_state_changed(self, model_run: ModelRun, previous_state: ModelRun.BuilderStatus | None, new_state: ModelRun.BuilderStatus):
        """Notify all observers about a build state change event."""
        for observer in self.observers:
            try:
                observer.on_build_state_changed(model_run, previous_state, new_state)
            except Exception as e:
                get_logger().error(f"Error notifying observer {type(observer)} about model build state change of model uid:{model_run.id}, model_id:{model_run.model_id}", exc_info=e)

    def notify_failure(self, model_run: ModelRun, failure):
        for observer in self.observers:
            try:
                observer.notify_failure(model_run, failure)
            except Exception as e:
                get_logger().error(f"Error notifying observer {type(observer)} about model failure of model uid:{model_run.id}, model_id:{model_run.model_id}", exc_info=e)
