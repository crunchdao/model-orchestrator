ARG BASE_PYTHON_IMAGE=python:3.13

FROM $BASE_PYTHON_IMAGE AS build

WORKDIR /app

COPY pyproject.toml poetry.lock README.md /app/
COPY model_orchestrator /app/model_orchestrator

RUN pip install --no-cache-dir poetry
RUN poetry build

FROM $BASE_PYTHON_IMAGE AS runtime

WORKDIR /app

COPY --from=build /app/dist/*.tar.gz /app/dist/*.whl /app/

RUN pip install --no-cache-dir *.whl

ENTRYPOINT ["model-orchestrator", "start"]
