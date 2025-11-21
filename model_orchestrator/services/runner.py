import abc
from model_orchestrator.entities.crunch import Crunch
from model_orchestrator.entities.model_run import ModelRun


class Runner(abc.ABC):

    @abc.abstractmethod
    def create(self, crunch: Crunch) -> dict:
        """
        create infrastructure for coming models of the Crunch

        Returns:
            dict: A dictionary containing the infrastructure details.
        """
        pass

    @abc.abstractmethod
    def run(self, model: ModelRun, crunch: Crunch) -> tuple[str, str, any]:
        """
        Runs the given model with the provided Crunch specifications.
    
        Args:
            model (ModelRun): The model to be run.
            crunch (Crunch): The configuration and resources for the model run.
    
        Returns:
            Tuple[str, str, str]: A tuple containing:
                - job_id (str): Identifier for the job running the model.
                - logs_arn (str): ARN (Amazon Resource Name) for accessing logs.
                - runner_settings (str): A custom object specific to the runner settings.
        """
        pass

    @abc.abstractmethod
    def stop(self, model: ModelRun) -> str:
        pass

    def load_status(self, model: ModelRun) -> tuple[ModelRun.RunnerStatus, str, int]:
        return self.load_statuses([model])[model]

    @abc.abstractmethod
    def load_statuses(self, model: list[ModelRun]) -> dict[ModelRun, tuple[ModelRun.RunnerStatus, str, int]]:
        pass
