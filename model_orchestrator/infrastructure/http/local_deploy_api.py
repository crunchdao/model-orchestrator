import base64
import os
import shutil
import asyncio
import urllib.parse
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Iterator, Optional, TYPE_CHECKING

import base58
import docker
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Query, Request, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, PlainTextResponse

from ...configuration import AppConfig
from ...infrastructure.config_watcher import ModelStateConfigYamlPolling
from ...mediators.models_state_mediator import ModelsStateMediator

if TYPE_CHECKING:
    from ...infrastructure.phala._cluster import PhalaCluster

from ._types import *
from ...utils.loader import import_notebook
from ...utils.logging_utils import get_logger
from ...utils.unique_slug import generate_unique_coolname

logger = get_logger()
_docker_client = None


def _get_docker_client():
    global _docker_client
    if _docker_client is None:
        _docker_client = docker.from_env()
    return _docker_client


async def process_uploaded_files(
    files: List[UploadFile],
    runner_config
) -> tuple[str, str]:
    notebook_file = None
    main_py = None
    requirements_txt = None

    for f in files:
        name = f.filename or ""

        if name.endswith(".ipynb"):
            notebook_file = f
            break  # notebook is enough, no need to keep scanning

        if name == "main.py":
            main_py = f
        elif name == "requirements.txt":
            requirements_txt = f

    has_raw_pair = (main_py is not None) and (requirements_txt is not None)
    if not (notebook_file or has_raw_pair):
        raise HTTPException(
            status_code=400,
            detail="Uploaded files must include either a Jupyter notebook (.ipynb) or both main.py and requirements.txt.",
        )

    submission_name = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
    submission_dir = runner_config.format_submission_storage_path(submission_name)

    if notebook_file:
        file_content = await notebook_file.read()
        import_notebook(file_content, submission_dir)
        logger.info("Imported to: %s", submission_dir)
    else:
        os.makedirs(submission_dir, exist_ok=True)
        for f in files:
            file_path = os.path.join(submission_dir, f.filename)
            with open(file_path, "wb") as outfile:
                outfile.write(await f.read())

    return submission_name, submission_dir


async def follow_file(path: str, follow: bool, from_start: bool):
    logger.debug(f"Following file {path}, with follow={follow}, from_start={from_start}")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if not os.path.exists(path):
        open(path, "ab").close()

    with open(path, "rb") as f:
        if not from_start:
            f.seek(0, 2)  # end
        while True:
            line = f.readline()
            if line:
                yield line
            else:
                if not follow:
                    break
                await asyncio.sleep(0.2)


def follow_container_logs(container_id: str, follow: bool, from_start: bool) -> Iterator[bytes]:
    container = _get_docker_client().containers.get(container_id)
    tail = "all" if from_start else 0

    # bytes chunks (stdout+stderr)
    yield from container.logs(
        stream=True,
        follow=follow,
        stdout=True,
        stderr=True,
        tail=tail,
    )


@dataclass
class LocalDeployServices:
    model_state_mediator: ModelsStateMediator
    app_config: AppConfig
    model_state_config: ModelStateConfigYamlPolling | None = None
    phala_cluster: "PhalaCluster | None" = None


def create_local_deploy_api(services: LocalDeployServices) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.services = services
        yield

    app = FastAPI(
        title="Local model deployment API",
        lifespan=lifespan
    )

    # Allow the frontend container to call the API (tighten in prod)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],  # set to your frontend origin(s)
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    def get_services(request: Request) -> LocalDeployServices:
        return request.app.state.services

    @app.post("/models", response_model=ModelResponse)
    async def add_model(
        svc: LocalDeployServices = Depends(get_services),
        desired_state: DesiredState = Form(DesiredState.RUNNING),
        crunch_id: Optional[str] = Form(None),
        model_name: Optional[str] = Form(None),
        cruncher_id: Optional[str] = Form(None),
        cruncher_name: Optional[str] = Form(None),
        files: List[UploadFile] = File(...),
    ):
        if svc.model_state_config is None:
            raise HTTPException(status_code=501, detail="Model upload not supported in this mode")

        runner_config = svc.app_config.infrastructure.runner

        submission_name, _ = await process_uploaded_files(files, runner_config)

        # for now, crunch_id is ignored and we use the first in the list
        if len(svc.app_config.crunches) == 0:
            raise HTTPException(status_code=400, detail="No crunch configured in orchestrator.dev.yml")

        crunch_id = svc.app_config.crunches[0].id
        model_id = str(svc.model_state_config.get_next_id())
        cruncher_id = cruncher_id or base58.b58encode(os.urandom(32)).decode("ascii")
        if not model_name or not cruncher_name:
            slug = generate_unique_coolname(svc.model_state_config.get_slugs())
            slug_split = slug.split("-")
            model_name = model_name or slug_split[0]
            cruncher_name = cruncher_name or slug_split[1]

        svc.model_state_config.append(str(model_id),
                                      submission_id=submission_name,
                                      crunch_id=crunch_id,
                                      desired_state=ModelRun.DesiredStatus(desired_state.value),
                                      cruncher_id = cruncher_id,
                                      model_name=model_name,
                                      cruncher_name=cruncher_name)

        logger.info("Model added in the YAML file, will be treated in the next polling cycle.")

        return ModelResponse(
            id=model_id,
            model_name=model_name,
            crunch_id=crunch_id,
            desired_state=desired_state,
            cruncher_id=cruncher_id,
            cruncher_name=cruncher_name
        )

    @app.patch("/models/{model_id}", response_model=ModelResponse)
    async def update_model(
        model_id: str,
        svc: LocalDeployServices = Depends(get_services),
        desired_state: DesiredState = Form(None),
        model_name: Optional[str] = Form(None),
        cruncher_name: Optional[str] = Form(None),
        files: Optional[List[UploadFile]] = File(None),
    ):

        if svc.model_state_config is None:
            raise HTTPException(status_code=501, detail="Model update not supported in this mode")

        model_config = svc.model_state_config.fetch_config(model_id)
        if not model_config:
            raise HTTPException(status_code=404, detail="Unknown model id")

        submission_name = None
        if files:
            runner_config = svc.app_config.infrastructure.runner
            submission_name, _ = await process_uploaded_files(files, runner_config)

        args = {}
        if desired_state is not None:
            args['desired_state'] = ModelRun.DesiredStatus(desired_state.value)
        if model_name is not None:
            args['model_name'] = model_name
        if cruncher_name is not None:
            args['cruncher_name'] = cruncher_name

        if submission_name:
            args['submission_id'] = submission_name

        if args:
            svc.model_state_config.update(model_id, **args)
            model_config = svc.model_state_config.fetch_config(model_id)

        return ModelResponse(
            id=model_id,
            model_name=model_config.get("model_name"),
            crunch_id=model_config.get("crunch_id"),
            desired_state=model_config.get("desired_state"),
            cruncher_id=model_config.get("cruncher_id"),
            cruncher_name=model_config.get("cruncher_name")
        )

    @app.get("/models", response_model=list[ModelListItem])
    def list_models(
        request: Request,
        svc: LocalDeployServices = Depends(get_services)
    ):
        items: list[ModelListItem] = []

        running_models = sorted(svc.model_state_mediator.get_all_models(), key=lambda model: model.model_id)

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
                    builder_log_uri=f"{request.url}/logs/{LogType.builder.value}/{base64.urlsafe_b64encode(model_run.builder_job_id.encode("utf-8")).decode("ascii")}" if model_run.builder_job_id else None,
                    runner_log_uri=f"{request.url}/logs/{LogType.runner.value}/{model_run.runner_job_id}" if model_run.runner_job_id else None
                )
            )

        return items

    @app.get("/models/logs/{type}/{job_id}")
    def stream_logs(
        type: LogType,
        job_id: str,
        follow: bool = Query(False),
        from_start: bool = Query(False),
        svc: LocalDeployServices = Depends(get_services),
    ):
        media_type = "text/plain; charset=utf-8"
        headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}

        # ── Phala mode: proxy to spawntee log endpoints ──
        if svc.phala_cluster is not None:
            return _proxy_phala_logs(svc.phala_cluster, type, job_id, follow, from_start)

        # ── Local mode: read from local files / Docker containers ──
        if type == LogType.builder:
            import tempfile
            temp_dir = tempfile.gettempdir()
            job_id_decoded = base64.urlsafe_b64decode(job_id.encode("ascii")).decode("utf-8")
            log_path = os.path.join(temp_dir, f"{job_id_decoded}.log")
            logger.debug(f"Streaming builder logs in {log_path}")
            return StreamingResponse(
                follow_file(log_path, follow=follow, from_start=from_start),
                media_type=media_type,
                headers=headers,
            )
        elif type == LogType.runner:
            return StreamingResponse(
                follow_container_logs(job_id, follow, from_start),
                media_type=media_type,
                headers=headers,
            )
        else:
            raise HTTPException(status_code=400, detail="Invalid log type")

    return app


def _proxy_phala_logs(
    cluster: "PhalaCluster",
    log_type: LogType,
    job_id: str,
    follow: bool,
    from_start: bool,
):
    """Proxy a log request to the spawntee CVM that owns the task."""
    from ..phala._client import SpawnteeClientError

    # For builder logs, job_id is base64-encoded task_id (same encoding
    # used in the URI construction in list_models)
    try:
        task_id = base64.urlsafe_b64decode(job_id.encode("ascii")).decode("utf-8") if log_type == LogType.builder else job_id
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid job id encoding")

    # Guard against path traversal via crafted base64 values
    if "/" in task_id or ".." in task_id:
        raise HTTPException(status_code=400, detail="Invalid job id")

    client = cluster.client_for_task(task_id)
    media_type = "text/plain; charset=utf-8"
    headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}

    try:
        if log_type == LogType.builder:
            response = client.get_builder_logs(
                task_id, follow=follow, from_start=from_start, stream=follow,
            )
        else:
            response = client.get_runner_logs(
                task_id, follow=follow, from_start=from_start, stream=follow,
            )

        if follow:
            # Stream chunks from spawntee → client without buffering
            def _iter_and_close():
                try:
                    for line in response.iter_lines(decode_unicode=True):
                        if line:
                            yield line + "\n"
                finally:
                    response.close()

            return StreamingResponse(
                _iter_and_close(),
                media_type=media_type,
                headers=headers,
            )

        return PlainTextResponse(
            content=response.text,
            media_type=media_type,
            headers=headers,
        )

    except SpawnteeClientError as e:
        status = e.status_code or 502
        raise HTTPException(status_code=status, detail=f"Failed to fetch logs from TEE: {e}")
