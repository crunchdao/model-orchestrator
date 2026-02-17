from model_orchestrator.entities import ModelRun
from model_orchestrator.services.model_runs import ModelRunsService


class ModelsStateMediator:
    def __init__(self, models_run_service: ModelRunsService):
        self.models_run_service = models_run_service

    def get_running_models(self) -> list[ModelRun]:
        return self.models_run_service.get_running_models()

    def get_running_models_by_crunch_id(self, crunch_id: str) -> list[ModelRun]:
        return self.models_run_service.get_running_models_by_crunch_id(crunch_id)

    def is_model_in_crunch(self, model_id: str, crunch_id: str) -> bool:
        return self.models_run_service.is_model_in_crunch(model_id, crunch_id)

    def get_all_models(self) -> list[ModelRun]:
        return self.models_run_service.get_all_models()


    def report_failure(self, failure_code: str, model_id: str, ip: str):
        return self.models_run_service.report_model_runner_failure(failure_code, model_id, ip)
