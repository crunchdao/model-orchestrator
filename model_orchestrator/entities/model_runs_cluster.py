from collections import defaultdict
from itertools import chain

from model_orchestrator.entities import ModelRun


class ModelRunsCluster:
    def __init__(self, models: list[ModelRun]):
        self.models_by_crunch: dict[str, list[ModelRun]] = defaultdict(list)
        for model in models:
            self.models_by_crunch[model.crunch_id].append(model)

    @property
    def models(self):
        return chain.from_iterable(self.models_by_crunch.values())

    def get_models_by_crunch_id(self, crunch_id: str) -> list[ModelRun]:
        return self.models_by_crunch.get(crunch_id, [])

    def get_models_by_model_id(self, model_id: str) -> list[ModelRun]:
        return [
            model
            for model in self.models
            if model.model_id == model_id
        ]

    def get_models_by_model_id_and_desired_status(self, model_id: str, desired_status: list[ModelRun.DesiredStatus]) -> list[ModelRun]:
        return [
            model
            for model in self.models
            if model.model_id == model_id and model.desired_status in desired_status
        ]

    def get_active_models_by_model_id(self, model_id: str) -> list[ModelRun]:
        return self.get_models_by_model_id_and_desired_status(model_id, [ModelRun.DesiredStatus.RUNNING, ModelRun.DesiredStatus.REPLACING])

    def get_running_models_by_model_id(self, model_id: str) -> list[ModelRun]:
        return self.get_models_by_model_id_and_desired_status(model_id, [ModelRun.DesiredStatus.RUNNING])

    def get_running_models(self, models=None) -> list[ModelRun]:
        return [
            model
            for model in models or self.models
            if model.desired_status in [ModelRun.DesiredStatus.RUNNING, ModelRun.DesiredStatus.REPLACING] and model.runner_status in [ModelRun.RunnerStatus.RUNNING]
        ]

    def get_running_models_by_crunch_id(self, crunch_id: str) -> list[ModelRun]:
        crunch_models = self.get_models_by_crunch_id(crunch_id)
        if not crunch_models:
            return []

        return self.get_running_models(crunch_models)

    def is_model_in_crunch(self, model_id: str, crunch_id: str) -> bool:
        return any(model.model_id == model_id for model in self.get_models_by_crunch_id(crunch_id))

    def get_models_in_run_phase(self) -> list[ModelRun]:
        return [
            model
            for model in self.models
            if model.in_run_phase()
        ]

    def get_models_in_build_phase(self) -> list[ModelRun]:
        return [
            model
            for model in self.models
            if model.in_build_phase()
        ]

    def add_model(self, model: ModelRun):
        self.models_by_crunch[model.crunch_id].append(model)

    def remove(self, model_run):
        crunch_models = self.models_by_crunch.get(model_run.crunch_id, [])
        if model_run in crunch_models:
            crunch_models.remove(model_run)
