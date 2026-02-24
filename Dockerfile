ARG BASE_PYTHON_IMAGE=python:3.13

FROM $BASE_PYTHON_IMAGE AS build

WORKDIR /app

COPY pyproject.toml poetry.lock README.md /app/
COPY model_orchestrator /app/model_orchestrator

RUN pip install --no-cache-dir poetry
RUN poetry build

FROM $BASE_PYTHON_IMAGE AS runtime

# Install Node.js and Phala CLI (needed for TEE CVM provisioning)
# Pin Node.js version and verify GPG key to avoid supply-chain risk from curl|bash
ARG NODE_MAJOR=22
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates gnupg \
    && mkdir -p /etc/apt/keyrings \
    && curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \
       | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg \
    && echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_${NODE_MAJOR}.x nodistro main" \
       > /etc/apt/sources.list.d/nodesource.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends nodejs \
    && npm install -g phala \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY --from=build /app/dist/*.tar.gz /app/dist/*.whl /app/
COPY entrypoint.sh /app/

RUN pip install --no-cache-dir *.whl
RUN chmod +x /app/entrypoint.sh

ENTRYPOINT ["./entrypoint.sh", "model-orchestrator", "start"]