from contextlib import asynccontextmanager
from dataclasses import dataclass

from fastapi import FastAPI, Request, Depends

from ...mediators.models_state_mediator import ModelsStateMediator

from ._types import *
from ...utils.logging_utils import get_logger

logger = get_logger()


@dataclass
class PhalaDeployServices:
    model_state_mediator: ModelsStateMediator


def create_phala_deploy_api(services: PhalaDeployServices) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.services = services
        yield

    app = FastAPI(
        title="Phala model deployment API",
        lifespan=lifespan,
    )

    def get_services(request: Request) -> PhalaDeployServices:
        return request.app.state.services

    @app.get("/models", response_model=list[ModelListItem])
    def list_models(
        svc: PhalaDeployServices = Depends(get_services),
    ):
        items: list[ModelListItem] = []

        running_models = sorted(
            svc.model_state_mediator.get_all_models(),
            key=lambda model: model.model_id,
        )

        for model_run in running_models:
            items.append(
                ModelListItem(
                    id=model_run.model_id,
                    model_name=model_run.augmented_info.name if model_run.augmented_info else None,
                    deployment_id=model_run.id,
                    desired_state=model_run.desired_status.value,
                    status=make_deployment_status(model_run),
                    statusMessage=model_run.failure.reason if model_run.failure else None,
                    crunch_id=model_run.crunch_id,
                    cruncher_id=model_run.cruncher_id,
                    cruncher_name=model_run.augmented_info.cruncher_name if model_run.augmented_info else None,
                    builder_log_uri=None,
                    runner_log_uri=None,
                )
            )

        return items

    return app
