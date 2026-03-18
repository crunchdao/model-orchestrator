from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from fastapi import FastAPI, Request, Depends, Query, HTTPException
from fastapi.responses import PlainTextResponse, StreamingResponse

from ...mediators.models_state_mediator import ModelsStateMediator

from ._types import *
from ...utils.logging_utils import get_logger

from ...infrastructure.phala._cluster import PhalaClusterError

if TYPE_CHECKING:
    from ...infrastructure.phala._cluster import PhalaCluster

logger = get_logger()


@dataclass
class PhalaDeployServices:
    model_state_mediator: ModelsStateMediator
    phala_cluster: PhalaCluster | None = field(default=None)


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
        request: Request,
        svc: PhalaDeployServices = Depends(get_services),
    ):
        items: list[ModelListItem] = []
        base_url = str(request.url).rstrip("/")

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
                    builder_log_uri=f"{base_url}/logs/{LogType.builder.value}/{model_run.builder_job_id}" if model_run.builder_job_id else None,
                    runner_log_uri=f"{base_url}/logs/{LogType.runner.value}/{model_run.runner_job_id}" if model_run.runner_job_id else None,
                )
            )

        return items

    @app.get("/models/logs/{log_type}/{task_id}")
    def stream_logs(
        log_type: LogType,
        task_id: str,
        follow: bool = Query(False),
        from_start: bool = Query(True),
        svc: PhalaDeployServices = Depends(get_services),
    ):
        if not svc.phala_cluster:
            raise HTTPException(status_code=503, detail="Phala cluster not available")

        try:
            client = svc.phala_cluster.client_for_task(task_id)
        except PhalaClusterError:
            raise HTTPException(status_code=404, detail="Task %s not found in cluster routing" % task_id)

        try:
            if log_type == LogType.builder:
                response = client.get_builder_logs(task_id, follow=follow, from_start=from_start, stream=follow)
            elif log_type == LogType.runner:
                response = client.get_runner_logs(task_id, follow=follow, from_start=from_start, stream=follow)
            else:
                raise HTTPException(status_code=400, detail="Invalid log type")
        except HTTPException:
            raise
        except Exception as e:
            error_msg = str(e)
            if "404" in error_msg:
                raise HTTPException(status_code=404, detail="No logs found for task %s" % task_id)
            logger.warning("Failed to fetch %s logs for task %s: %s", log_type.value, task_id, error_msg)
            raise HTTPException(status_code=502, detail="Failed to fetch logs from CVM")

        media_type = "application/x-ndjson"
        headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}

        if follow:
            def iter_lines():
                try:
                    for line in response.iter_lines():
                        if line:
                            yield line.decode("utf-8", errors="replace") + "\n"
                finally:
                    response.close()

            return StreamingResponse(iter_lines(), media_type=media_type, headers=headers)

        return PlainTextResponse(response.text, media_type=media_type, headers=headers)

    return app
