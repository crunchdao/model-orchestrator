from abc import abstractmethod

from model_orchestrator.entities import ModelInfo


class AugmentedModelInfoRepository:

    @abstractmethod
    def loads(self, models_id: list[str]) -> dict[str, ModelInfo]:
        pass
    
    @abstractmethod
    def load_storage_references(self, submission_id: str) -> dict | None:
        pass

class DisabledAugmentedModelInfoRepository(AugmentedModelInfoRepository):
    def loads(self, models_id: list[str]) -> dict[str, ModelInfo]:
        return {}

    def load_storage_references(self, submission_id: str) -> dict | None:
        return {}