from model_orchestrator.entities import ModelRun


class ModelRunsCluster:
    def __init__(self, models: list[ModelRun]):
        self.models = models

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

    def get_running_models(self) -> list[ModelRun]:
        return [
            model
            for model in self.models
            if model.desired_status in [ModelRun.DesiredStatus.RUNNING, ModelRun.DesiredStatus.REPLACING] and model.runner_status in [ModelRun.RunnerStatus.RUNNING]
        ]

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
        self.models.append(model)

    def remove(self, model_run):
        if model_run in self.models:
            self.models.remove(model_run)
