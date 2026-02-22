import abc

from model_orchestrator.entities.crunch import Crunch
from model_orchestrator.entities.model_run import ModelRun


class Builder(abc.ABC):

    @abc.abstractmethod
    def create(self, crunch: Crunch) -> dict:
        """
        Create infrastructure for upcoming models of the Crunch.

        Returns:
            dict: A dictionary containing the infrastructure details.
        """

        pass

    def load_status(self, model: ModelRun) -> ModelRun.BuilderStatus:
        return self.load_statuses([model])[model]

    @abc.abstractmethod
    def load_statuses(self, models: list[ModelRun]) -> dict[ModelRun, ModelRun.BuilderStatus]:
        pass

    @abc.abstractmethod
    def build(self, model: ModelRun, crunch: Crunch) -> tuple[str, str, str]:
        """
         Args:
            model (ModelRun): The ModelRun object containing model information.
            crunch (Crunch): The Crunch object containing crunch information.

        Returns:
            Tuple[str, str, str]: A tuple containing:
                - job_id (str): The identifier of the created job.
                - docker_tag (str): The docker tag associated with the Crunch.
                - logs_arn (str): The ARN for the logs generated during the process.
        """
        pass

    @abc.abstractmethod
    def is_built(self, model: ModelRun, crunch: Crunch) -> tuple[bool, str]:
        pass
