# TEE Cluster Initialization (current)

## Overview

The Phala TEE setup now works as a **registry-first cluster**:

- Start with **one registry CVM** (`registry+runner` mode).
- The orchestrator discovers CVMs by `cluster-name` prefix.
- When capacity is exhausted, the orchestrator auto-provisions runner CVMs (`runner` mode).
- Runner attestation hashes are approved dynamically by the orchestrator via:
  - `POST /registry/approve-hash`

> ✅ Important: the old manual `APPROVED_COMPOSE_HASH` bootstrap flow is no longer the primary path for current spawntee versions.

---

## What changed vs the old doc

The previous version of this doc described a required temporary-runner flow:

1. Deploy temp runner
2. Read `compose_hash`
3. Delete temp runner
4. Re-deploy registry with `APPROVED_COMPOSE_HASH`

That flow is now legacy. Current orchestrator code pushes runner hashes to the registry automatically after discovery/provisioning.

---

## Prerequisites

### Required repos/layout

This repo expects `cruncher_phala` as a sibling when using the setup script:

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

## Recommended setup path

Use:

```bash
scripts/setup_phala_cluster.sh
```

The script currently:

1. Validates environment and required code changes
2. Collects credentials and cluster settings
3. Writes secret/env files
4. Optionally builds/pushes spawntee image
5. Deploys (or reuses) registry CVM
6. Performs compose-hash attestation setup
7. Writes orchestrator `.env`
8. Verifies health and auth behavior

> Note: the script still contains a compatibility step that can set `APPROVED_COMPOSE_HASH`. This is safe, but for current spawntee/orchestrator behavior hash approval is also handled dynamically at runtime.

---

## Minimal manual bootstrap (current runtime model)

If you do this manually, the minimum needed state is:

1. Deploy a registry CVM
2. Configure orchestrator with Phala runner + gateway certs
3. Start orchestrator

### 1) Deploy registry CVM

Use the compose from `cruncher_phala` (current path used by setup script):

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

Set env vars (for runtime + autoscaling):

```bash
export PHALA_API_KEY=...
export GATEWAY_CERT_DIR=/path/to/coordinator/certs
export GATEWAY_AUTH_COORDINATOR_WALLET=...
```

Use Phala runner config (kebab-case in YAML):

```yaml
infrastructure:
  runner:
    type: phala
    cluster-name: "crunch-tee"
    phala-api-url: "https://cloud-api.phala.network"
    runner-compose-path: "../cruncher_phala/spawntee/docker-compose.phala.runner.yml"
    spawntee-port: 9010
    request-timeout: 60
    instance-type: "tdx.medium"
    max-models: 0
    gateway-cert-dir: "/path/to/coordinator/certs"
```

### 3) Start orchestrator

```bash
poetry run model-orchestrator
```

---

## Runtime behavior (current)

At startup, `PhalaCluster`:

1. Discovers CVMs by `cluster-name` prefix from Phala API
2. Probes each CVM for mode/health
3. Picks head CVM based on `/capacity`
4. Rebuilds task routing map from `/running_models`
5. Pushes discovered runner compose hashes to registry via `/registry/approve-hash`

During operation:

- Before each build, orchestrator asks head CVM `/capacity`
- If full, it provisions a new runner with `phala deploy`
- Waits for runner readiness (`/` then `/capacity`)
- Approves runner hash on registry
- Promotes new runner as head

---

## Notes / caveats

- `runner-compose-path` is required for autoscaling.
- `phala` CLI must be available on the orchestrator host for provisioning.
- `PHALA_API_KEY` is read from environment (not YAML field).
- `memory-per-model-mb` and `provision-factor` are still accepted in config for backward compatibility but currently ignored by `PhalaCluster` capacity decisions.
- If you run older spawntee that does not support `/registry/approve-hash`, you may need the legacy `APPROVED_COMPOSE_HASH` flow.
