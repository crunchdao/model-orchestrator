from abc import abstractmethod

from model_orchestrator.entities import ModelInfo


class AugmentedModelInfoRepository:

    @abstractmethod
    def loads(self, models_id: list[str]) -> dict[str, ModelInfo]:
        pass


class DisabledAugmentedModelInfoRepository(AugmentedModelInfoRepository):
    def loads(self, models_id: list[str]) -> dict[str, ModelInfo]:
        return {}
