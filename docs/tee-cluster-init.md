# TEE Cluster Initialization

## Overview

The Phala TEE setup works as a **registry-first cluster**:

- Start with **one registry CVM** (`registry+runner` mode).
- The orchestrator discovers CVMs by `cluster-name` prefix.
- When capacity is exhausted, the orchestrator auto-provisions runner CVMs (`runner` mode).
- Runner attestation hashes are approved on the registry dynamically by the orchestrator via `POST /registry/approve-hash`.

No manual compose-hash bootstrapping is needed — the orchestrator reads each runner's `compose_hash` from the Phala API and pushes it to the registry automatically.

---

## Prerequisites

### Required repos/layout

This repo expects `cruncher_phala` as a sibling:

```text
some-parent/
  ├── model-orchestrator-new/
  └── cruncher_phala/
```

### Required tools

- `phala` CLI
- `python3`
- `curl`
- `openssl`

### Required credentials / env

- `PHALA_API_KEY`
- `AWS_ACCESS_KEY_ID` (registry CVM)
- `AWS_SECRET_ACCESS_KEY` (registry CVM)
- `AWS_REGION` (default: `eu-west-1`)
- Coordinator RSA key directory (`key.pem` or `tls.key`) for gateway auth signing
- `GATEWAY_AUTH_COORDINATOR_WALLET` (wallet used by spawntee gateway auth middleware)

---

## Setup script

Use:

```bash
scripts/setup_phala_cluster.sh
```

The script:

1. Validates environment and required code changes
2. Collects credentials and cluster settings
3. Writes secret/env files
4. Optionally builds/pushes spawntee image
5. Deploys (or reuses) registry CVM
6. Writes orchestrator `.env`
7. Verifies health and auth behavior

---

## Manual bootstrap

### 1) Deploy registry CVM

```bash
phala deploy \
  --name "${CLUSTER_NAME}-registry" \
  --instance-type "${INSTANCE_TYPE:-tdx.medium}" \
  --compose "../cruncher_phala/spawntee/docker-compose.phala.debug.yml" \
  -e "AWS_ACCESS_KEY_ID=$AWS_ACCESS_KEY_ID" \
  -e "AWS_SECRET_ACCESS_KEY=$AWS_SECRET_ACCESS_KEY" \
  -e "AWS_REGION=${AWS_REGION:-eu-west-1}" \
  -e "GATEWAY_AUTH_COORDINATOR_WALLET=$GATEWAY_AUTH_COORDINATOR_WALLET" \
  --api-key "$PHALA_API_KEY" \
  --json --wait
```

### 2) Configure orchestrator

Set env vars:

```bash
export PHALA_API_KEY=...
export GATEWAY_CERT_DIR=/path/to/coordinator/certs
export GATEWAY_AUTH_COORDINATOR_WALLET=...
```

Phala runner config (YAML):

```yaml
infrastructure:
  runner:
    type: phala
    cluster-name: "crunch-tee"
    phala-api-url: "https://cloud-api.phala.network"
    runner-compose-path: "../cruncher_phala/spawntee/docker-compose.phala.runner.yml"
    spawntee-port: 9010
    request-timeout: 30
    instance-type: "tdx.medium"
    max-models: 0
    memory-per-model-mb: 1024
    gateway-cert-dir: "/path/to/coordinator/certs"
```

### 3) Start orchestrator

```bash
model-orchestrator
```

The orchestrator handles runner provisioning and compose-hash registration automatically from here.

---

## Runtime behavior

At startup, `PhalaCluster`:

1. Discovers CVMs by `cluster-name` prefix from Phala API
2. Probes each CVM for mode/health (`GET /`)
3. Picks head CVM based on `/capacity` (`accepting_new_models`)
4. Rebuilds task routing map from `/running_models`
5. Reads runner compose hashes from Phala API and pushes them to registry via `POST /registry/approve-hash`

During operation:

- Before each build, orchestrator asks head CVM `GET /capacity`
- If full, it checks other CVMs for capacity, then provisions a new runner with `phala deploy` if none have space
- New runners are deployed with `REGISTRY_URL`, `CAPACITY_THRESHOLD`, `MODEL_MEMORY_LIMIT_MB`, and `GATEWAY_AUTH_COORDINATOR_WALLET` env vars
- Waits for runner readiness (`GET /` then `GET /capacity`)
- Reads the new runner's compose hash from the Phala API and approves it on the registry via `POST /registry/approve-hash`
- Promotes new runner as head

---

## Config reference

| YAML field | Default | Description |
|---|---|---|
| `cluster-name` | `""` | Name prefix for CVM discovery |
| `phala-api-url` | `https://cloud-api.phala.network` | Phala Cloud API base URL |
| `runner-compose-path` | `""` | Path to runner docker-compose for auto-provisioning |
| `spawntee-port` | `9010` | Port where spawntee API listens |
| `request-timeout` | `30` | HTTP timeout for spawntee API calls (seconds) |
| `instance-type` | `tdx.medium` | Phala CVM instance type for new runners |
| `max-models` | `0` | Global cap on total models (0 = unlimited) |
| `memory-per-model-mb` | `1024` | Memory per model in MB. Passed as `MODEL_MEMORY_LIMIT_MB` to provisioned runners for capacity planning and container memory limits. |
| `provision-factor` | `0.8` | Accepted for backward compatibility, not used at runtime |
| `gateway-cert-dir` | `null` | Path to coordinator cert dir (key.pem or tls.key). Also settable via `GATEWAY_CERT_DIR` env var. |

Environment variables (not in YAML):

| Variable | Description |
|---|---|
| `PHALA_API_KEY` | Phala Cloud API key |
| `GATEWAY_CERT_DIR` | Fallback for `gateway-cert-dir` YAML field |
| `GATEWAY_AUTH_COORDINATOR_WALLET` | Coordinator wallet for spawntee gateway auth |
| `CAPACITY_THRESHOLD` | Passed to provisioned runners (default `0.8`) |
