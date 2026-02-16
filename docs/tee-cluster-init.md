# TEE Cluster Initialization

## Overview

A TEE cluster consists of Phala CVMs (Confidential Virtual Machines) running
the spawntee service. The cluster has two CVM roles:

- **Registry** (`registry+runner` mode) — holds encryption keys, runs models,
  and validates runners via TDX attestation + compose hash verification. Needs
  AWS credentials for S3 access to encrypted models. This is the only CVM
  after initialization.

- **Runner** (`runner` mode) — additional CVMs auto-provisioned by the
  orchestrator when the registry reaches capacity. Pulls model data from the
  registry via `/registry/re-encrypt`. No AWS credentials needed.

## Initialization Flow

```
1. Verify prerequisites
2. Collect inputs
3. Deploy registry CVM
4. Wait for registry to become healthy
5. Deploy a temporary runner CVM (to obtain the compose hash)
6. Get the runner's compose_hash from the Phala API
7. Delete the temporary runner CVM
8. Update registry with APPROVED_COMPOSE_HASH
9. Verify registry is healthy
10. Generate orchestrator config YAML
```

**Why a temporary runner is needed:** The registry verifies runner identity via
TDX attestation, which includes a `compose_hash`. This hash is computed by the
Phala platform — it's *not* a simple local hash of the compose file. The only
way to obtain it is to deploy a CVM with that compose file and read the hash
back from the API. The temporary runner is deleted immediately after to avoid
paying for an idle CVM. Until `APPROVED_COMPOSE_HASH` is set on the registry,
it rejects all runner attestation requests (fail-closed).

## Prerequisites

| Requirement | Check command |
|---|---|
| Phala CLI installed | `phala --version` |
| Phala CLI logged in | `phala status` |
| `PHALA_API_KEY` set | `echo $PHALA_API_KEY` |
| AWS credentials available | For registry CVM's S3 access |
| Compose files present | `model_orchestrator/data/docker-compose.phala.{registry,runner}.yml` |

## Required Inputs

| Input | Description | Example |
|---|---|---|
| **Cluster name** | Name prefix for all CVMs. Used for API discovery. | `bird-tracker` |
| **AWS_ACCESS_KEY_ID** | S3 access for encrypted model storage (registry only) | `AKIA...` |
| **AWS_SECRET_ACCESS_KEY** | S3 secret key (registry only) | `wJal...` |

## Inputs with Defaults

| Input | Default | Description |
|---|---|---|
| Instance type | `tdx.medium` | CVM size: `tdx.small` (2GB), `tdx.medium` (4GB), `tdx.large` (8GB), `tdx.xlarge` (16GB) |
| AWS_REGION | `eu-west-1` | S3 region |
| Spawntee port | `9010` | Port where spawntee API is exposed |
| Memory per model | `1024` MB | Estimated RAM per model container |
| Provision factor | `0.8` | Provision new runner at this % of capacity |
| Max models | `0` (unlimited) | Global cap across the cluster |
| CAPACITY_THRESHOLD | `0.8` | CVM reports full at this usage ratio |
| CVM_BASE_DOMAIN | `dstack-pha-prod10.phala.network` | Phala gateway domain |
| Registry compose | `model_orchestrator/data/docker-compose.phala.registry.yml` | |
| Runner compose | `model_orchestrator/data/docker-compose.phala.runner.yml` | |

## Outputs

After successful initialization:

1. **Registry CVM** — deployed, healthy, `APPROVED_COMPOSE_HASH` set, ready to run models and accept future runners
2. **Orchestrator config YAML** — ready to run with the registry as the sole CVM

## Step-by-Step Detail

### Step 1: Deploy Registry CVM

```bash
phala deploy \
  --name {cluster_name}-registry \
  --instance-type {instance_type} \
  --compose {registry_compose_path} \
  -e AWS_ACCESS_KEY_ID={aws_key} \
  -e AWS_SECRET_ACCESS_KEY={aws_secret} \
  -e AWS_REGION={aws_region} \
  --json --wait
```

The registry starts in `registry+runner` mode (from compose `MODE=${MODE:-registry+runner}`).
`APPROVED_COMPOSE_HASH` is empty at this point — runners cannot attest yet.

### Step 2: Verify Registry Health

```bash
curl https://{registry_app_id}-{spawntee_port}.dstack-pha-{node_name}.phala.network/health
# → {"status": "healthy", "mode": "registry+runner", ...}
```

### Step 3: Deploy Temporary Runner CVM

Deploy a throwaway runner solely to obtain the compose hash from the platform.

```bash
phala deploy \
  --name {cluster_name}-runner-temp \
  --instance-type {instance_type} \
  --compose {runner_compose_path} \
  -e REGISTRY_URL=https://{registry_app_id}-{spawntee_port}.dstack-pha-{node_name}.phala.network \
  --json --wait
```

### Step 4: Get Runner Compose Hash

```bash
curl -H "X-API-Key: $PHALA_API_KEY" \
  https://cloud-api.phala.network/api/v1/cvms/{runner_app_id} \
  | jq '.compose_hash'
```

### Step 5: Delete Temporary Runner

```bash
phala cvms delete {runner_app_id}
```

### Step 6: Update Registry with APPROVED_COMPOSE_HASH

Re-deploy the registry with the runner's compose hash so it accepts future runner attestations:

```bash
phala deploy \
  --uuid {registry_uuid} \
  --compose {registry_compose_path} \
  -e AWS_ACCESS_KEY_ID={aws_key} \
  -e AWS_SECRET_ACCESS_KEY={aws_secret} \
  -e AWS_REGION={aws_region} \
  -e APPROVED_COMPOSE_HASH={runner_compose_hash} \
  --json --wait
```

### Step 7: Verify Registry Health

```bash
curl https://{registry}-{spawntee_port}.../health
# → {"status": "healthy", "mode": "registry+runner"}
```

### Step 8: Generate Orchestrator Config

Produce an `orchestrator.yml` with:
- `cluster-name`, `phala-api-url`, compose paths
- `cluster-urls` fallback (registry URL)
- Instance type, memory, provision settings
- Watcher config and crunch definitions

## Important Notes

- **Compose hash changes with every compose file change** (including image version bumps).
  After updating compose files, deploy a temporary runner, grab the new hash, delete it,
  and update `APPROVED_COMPOSE_HASH` on the registry.
- **Multiple hashes** can be approved for rolling upgrades: `hash_old,hash_new`.
- After init, the orchestrator auto-provisions dedicated runners via
  `_provision_new_runner()` as the registry reaches capacity.
- `allowed_envs` in the Phala API ensures env var values don't affect the compose hash,
  so runners with different `REGISTRY_URL` values share the same hash.
