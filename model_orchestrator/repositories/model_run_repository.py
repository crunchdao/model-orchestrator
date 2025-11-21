import abc
from typing import Dict
from model_orchestrator.entities.model_run import ModelRun


class ModelRunRepository(abc.ABC):
    """
    Defines the contract for any Model repository (SQL, NoSQL, etc.).
    """

    @abc.abstractmethod
    def load_active(self) -> list[ModelRun]:
        """
        Loads from the data source all models that are NOT (STOPPED/STOPPED).
        """
        pass

    @abc.abstractmethod
    def save_model(self, model: ModelRun) -> None:
        """
        Insert or update the given model in the data source.
        """
        pass

    @abc.abstractmethod
    def delete_model(self, model_id: str) -> None:
        """
        Delete a model from the data source.
        """
        pass

    @abc.abstractmethod
    def is_model_in_quarantine(self, model_id: str, code_submission_id: str, resource_id: str, hardware_type: ModelRun.HardwareType) -> bool:
        pass