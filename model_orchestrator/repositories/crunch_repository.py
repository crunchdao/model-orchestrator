import abc

from model_orchestrator.entities.crunch import Crunch


class CrunchRepository(abc.ABC):
    """
    Defines the contract for any Crunch repository (SQL, NoSQL, etc.).
    """

    @abc.abstractmethod
    def load_active(self) -> list[Crunch]:
        pass

    @abc.abstractmethod
    def get(self, id: str) -> Crunch | None:
        pass

    @abc.abstractmethod
    def save(self, model: Crunch) -> None:
        """
        Insert or update the given crunch in the data source.
        """
        pass

    @abc.abstractmethod
    def delete(self, crunch_id: str) -> None:
        """
        Delete a crunch from the data source.
        """
        pass
