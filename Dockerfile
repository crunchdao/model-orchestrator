ARG BASE_PYTHON_IMAGE=python:3.13

FROM $BASE_PYTHON_IMAGE AS build

WORKDIR /app

COPY pyproject.toml poetry.lock README.md /app/
COPY model_orchestrator /app/model_orchestrator

RUN pip install --no-cache-dir poetry
RUN poetry build

FROM $BASE_PYTHON_IMAGE AS runtime

# Install Node.js and Phala CLI (needed for TEE CVM provisioning)
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates gnupg \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && npm install -g phala \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY --from=build /app/dist/*.tar.gz /app/dist/*.whl /app/

RUN pip install --no-cache-dir *.whl

ENTRYPOINT ["model-orchestrator", "start"]
