# Reproducible CVM Deploy Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make CVM deployment fully reproducible by requiring all env vars explicitly (no hidden defaults) and selecting the Phala node upfront so CVM_BASE_DOMAIN is known at deploy time.

**Architecture:** The setup script queries available Phala nodes, the user picks one, and all env vars (including CVM_BASE_DOMAIN derived from the node name) are passed in a single `phala deploy` call. Compose files and Python code are stripped of dangerous defaults for secrets/infrastructure vars.

**Tech Stack:** Bash (setup script), Docker Compose YAML, Python (spawntee service)

---

### Task 1: Strip defaults from compose files

**Files:**
- Modify: `/Users/borisnieuwenhuis/projects/crunch/cvm-phala-cruncher/spawntee/docker-compose.phala.debug.yml:121,123`
- Modify: `/Users/borisnieuwenhuis/projects/crunch/cvm-phala-cruncher/spawntee/docker-compose.phala.runner.yml:134`
- Modify: `/Users/borisnieuwenhuis/projects/crunch/cvm-phala-cruncher/spawntee/docker-compose.phala.registry.vpc.yml:146,148`
- Modify: `/Users/borisnieuwenhuis/projects/crunch/cvm-phala-cruncher/spawntee/docker-compose.phala.runner.vpc.yml:141`
- Modify: `/Users/borisnieuwenhuis/projects/crunch/cvm-phala-cruncher/spawntee/docker-compose.minimal.yml:17`

These vars must become required (no `:-default`):
- `AWS_REGION` — change `${AWS_REGION:-eu-west-1}` to `${AWS_REGION}`
- `GATEWAY_AUTH_COORDINATOR_WALLET` — change `${GATEWAY_AUTH_COORDINATOR_WALLET:-}` to `${GATEWAY_AUTH_COORDINATOR_WALLET}`
- `CVM_BASE_DOMAIN` — already done in debug.yml, runner.yml, both vpc files. Verify no regressions.

**Step 1: Edit all compose files**

In every file above, replace:
```yaml
- AWS_REGION=${AWS_REGION:-eu-west-1}
```
with:
```yaml
- AWS_REGION=${AWS_REGION}
```

And replace:
```yaml
- GATEWAY_AUTH_COORDINATOR_WALLET=${GATEWAY_AUTH_COORDINATOR_WALLET:-}
```
with:
```yaml
- GATEWAY_AUTH_COORDINATOR_WALLET=${GATEWAY_AUTH_COORDINATOR_WALLET}
```

**Step 2: Verify no `:-` defaults remain for secret/infra vars**

Run:
```bash
cd /Users/borisnieuwenhuis/projects/crunch/cvm-phala-cruncher/spawntee
grep -n 'AWS_REGION.*:-\|GATEWAY_AUTH_COORDINATOR_WALLET.*:-\|CVM_BASE_DOMAIN.*:-\|AWS_ACCESS_KEY_ID.*:-\|AWS_SECRET_ACCESS_KEY.*:-' docker-compose.phala.*.yml docker-compose.minimal.yml
```
Expected: no output

**Step 3: Commit**

```bash
cd /Users/borisnieuwenhuis/projects/crunch/cvm-phala-cruncher
git add spawntee/docker-compose.phala.debug.yml spawntee/docker-compose.phala.runner.yml spawntee/docker-compose.phala.registry.vpc.yml spawntee/docker-compose.phala.runner.vpc.yml spawntee/docker-compose.minimal.yml
git commit -m "Remove hidden defaults from secret/infra env vars in compose files"
```

---

### Task 2: Make Python code fail loudly on missing required env vars

**Files:**
- Modify: `/Users/borisnieuwenhuis/projects/crunch/cvm-phala-cruncher/spawntee/src/model_service.py:92,127-131` (CVM_BASE_DOMAIN — already done, verify)
- Modify: `/Users/borisnieuwenhuis/projects/crunch/cvm-phala-cruncher/spawntee/src/main.py:65` (GATEWAY_AUTH_COORDINATOR_WALLET)

**Step 1: Verify model_service.py CVM_BASE_DOMAIN is already fixed**

Read `/Users/borisnieuwenhuis/projects/crunch/cvm-phala-cruncher/spawntee/src/model_service.py` around line 92 and 127. Confirm:
- `DEFAULT_BASE_DOMAIN = ""` (not `"dstack-pha-prod10.phala.network"`)
- Critical log if empty

**Step 2: Fix GATEWAY_AUTH_COORDINATOR_WALLET in main.py**

In `/Users/borisnieuwenhuis/projects/crunch/cvm-phala-cruncher/spawntee/src/main.py:65`, change:
```python
GATEWAY_AUTH_COORDINATOR_WALLET = os.getenv("GATEWAY_AUTH_COORDINATOR_WALLET", "")
```
to:
```python
GATEWAY_AUTH_COORDINATOR_WALLET = os.getenv("GATEWAY_AUTH_COORDINATOR_WALLET", "")
if not GATEWAY_AUTH_COORDINATOR_WALLET:
    logger.warning("GATEWAY_AUTH_COORDINATOR_WALLET is not set — gateway auth is disabled")
```

Note: this one stays as a warning (not critical) because gateway auth is optional in some modes.

**Step 3: Commit**

```bash
cd /Users/borisnieuwenhuis/projects/crunch/cvm-phala-cruncher
git add spawntee/src/main.py spawntee/src/model_service.py
git commit -m "Log warnings when required env vars are missing"
```

---

### Task 3: Add node selection to setup script

This is the main change. Replace the post-deploy node discovery + redeploy with upfront node selection.

**Files:**
- Modify: `/Users/borisnieuwenhuis/projects/crunch/model-orchestrator/scripts/setup_phala_cluster.sh`

**Step 1: Add node selection after Phala API key collection (after line 187)**

Insert a new section that:
1. Queries `https://cloud-api.phala.network/api/v1/teepods/available` using the API key
2. Displays available nodes with name and region
3. User picks one by entering the node name
4. Computes `NODE_ID` (teepod_id) and `CVM_BASE_DOMAIN`

Add after the PHALA_API_KEY block (line 187) and before the CLUSTER_NAME block (line 191):

```bash
echo ""

# Node selection (determines CVM_BASE_DOMAIN)
if [[ -z "${PHALA_NODE_NAME:-}" ]]; then
    echo "Querying available Phala nodes..."
    NODE_LIST=$(curl -sf -H "accept: application/json" -H "x-api-key: $PHALA_API_KEY" \
        "https://cloud-api.phala.network/api/v1/teepods/available" 2>/dev/null \
        | python3 -c "
import sys, json
data = json.load(sys.stdin)
nodes = data.get('nodes', [])
for n in nodes:
    print(f'{n[\"name\"]}|{n[\"teepod_id\"]}|{n.get(\"region_identifier\", \"?\")}')
" 2>/dev/null) || NODE_LIST=""

    if [[ -z "$NODE_LIST" ]]; then
        err "Could not query available nodes. Check PHALA_API_KEY."
        exit 1
    fi

    echo ""
    echo "Available nodes:"
    while IFS='|' read -r name tid region; do
        echo "  $name  (region: $region, teepod_id: $tid)"
    done <<< "$NODE_LIST"
    echo ""

    PHALA_NODE_NAME=$(ask "Node name: ")
    if [[ -z "$PHALA_NODE_NAME" ]]; then
        err "Node selection is required"
        exit 1
    fi

    # Look up teepod_id for chosen node
    PHALA_NODE_ID=$(echo "$NODE_LIST" | while IFS='|' read -r name tid region; do
        if [[ "$name" == "$PHALA_NODE_NAME" ]]; then echo "$tid"; break; fi
    done)

    if [[ -z "$PHALA_NODE_ID" ]]; then
        err "Unknown node: $PHALA_NODE_NAME"
        exit 1
    fi
else
    info "PHALA_NODE_NAME is set: $PHALA_NODE_NAME"
    # Look up teepod_id
    PHALA_NODE_ID=$(curl -sf -H "accept: application/json" -H "x-api-key: $PHALA_API_KEY" \
        "https://cloud-api.phala.network/api/v1/teepods/available" 2>/dev/null \
        | python3 -c "
import sys, json
data = json.load(sys.stdin)
for n in data.get('nodes', []):
    if n['name'] == '$PHALA_NODE_NAME':
        print(n['teepod_id']); break
" 2>/dev/null) || PHALA_NODE_ID=""
    if [[ -z "$PHALA_NODE_ID" ]]; then
        err "Could not find teepod_id for node $PHALA_NODE_NAME"
        exit 1
    fi
fi

CVM_BASE_DOMAIN="dstack-pha-${PHALA_NODE_NAME}.phala.network"
info "Node: $PHALA_NODE_NAME (teepod_id=$PHALA_NODE_ID, domain=$CVM_BASE_DOMAIN)"
```

**Step 2: Add node info to configuration summary and saved config**

Add to the summary block (around line 222):
```
echo "  NODE:              $PHALA_NODE_NAME ($CVM_BASE_DOMAIN)"
```

Add to saved config (around line 235):
```
PHALA_NODE_NAME=$PHALA_NODE_NAME
PHALA_NODE_ID=$PHALA_NODE_ID
CVM_BASE_DOMAIN=$CVM_BASE_DOMAIN
```

**Step 3: Update Phase 5 deploy to use node and CVM_BASE_DOMAIN**

Replace the deploy command (lines 353-368) to add `--node-id` and `-e CVM_BASE_DOMAIN`:

```bash
    REGISTRY_DEPLOY_OUT=$(phala deploy \
        --name "$REGISTRY_NAME" \
        --instance-type "$INSTANCE_TYPE" \
        --node-id "$PHALA_NODE_ID" \
        --compose "$PHALA_ROOT/spawntee/docker-compose.phala.debug.yml" \
        -e "AWS_ACCESS_KEY_ID=$AWS_ACCESS_KEY_ID" \
        -e "AWS_SECRET_ACCESS_KEY=$AWS_SECRET_ACCESS_KEY" \
        -e "AWS_REGION=$AWS_REGION" \
        -e "GATEWAY_AUTH_COORDINATOR_WALLET=$GATEWAY_AUTH_COORDINATOR_WALLET" \
        -e "CVM_BASE_DOMAIN=$CVM_BASE_DOMAIN" \
        --api-key "$PHALA_API_KEY" \
        --json \
        --wait \
        2>&1) || {
        err "Registry CVM deploy failed"
        echo "$REGISTRY_DEPLOY_OUT"
        exit 1
    }
```

**Step 4: Remove the post-deploy node discovery and redeploy**

Delete everything from `# Get node_name for URL construction` through `info "CVM_BASE_DOMAIN set to $REGISTRY_DOMAIN"` (the old lines 401-435). Replace with:

```bash
REGISTRY_URL="https://${REGISTRY_APP_ID}-9010.${CVM_BASE_DOMAIN}"
```

**Step 5: Update the reuse path for existing registry CVM**

When the registry CVM already exists (line 344-345), the `REGISTRY_URL` also needs to be set. Change:
```bash
if [[ -n "$REGISTRY_APP_ID" ]]; then
    info "Registry CVM already exists: app_id=$REGISTRY_APP_ID (reusing)"
```
to:
```bash
if [[ -n "$REGISTRY_APP_ID" ]]; then
    info "Registry CVM already exists: app_id=$REGISTRY_APP_ID (reusing)"
    REGISTRY_URL="https://${REGISTRY_APP_ID}-9010.${CVM_BASE_DOMAIN}"
```

**Step 6: Verify the script is syntactically correct**

Run:
```bash
bash -n /Users/borisnieuwenhuis/projects/crunch/model-orchestrator/scripts/setup_phala_cluster.sh
```
Expected: no output (no syntax errors)

**Step 7: Commit**

```bash
cd /Users/borisnieuwenhuis/projects/crunch/model-orchestrator
git add scripts/setup_phala_cluster.sh
git commit -m "Add upfront node selection to setup script — CVM_BASE_DOMAIN known at deploy time"
```

---

### Task 4: Verify end-to-end on staging

**Step 1: Deploy a new registry CVM using the updated script**

From local machine (where both repos are checked out):
```bash
cd /Users/borisnieuwenhuis/projects/crunch/model-orchestrator
# Set env vars from previous deployment:
export AWS_ACCESS_KEY_ID=AKIA2TOKSIU3ATFNRQ7K
export AWS_SECRET_ACCESS_KEY=<value>
export AWS_REGION=eu-west-1
export PHALA_API_KEY=phak_77TuME2zzxg_bUDfqvUlV_pktuVbZCklI-0Zw-JS28U
export GATEWAY_AUTH_COORDINATOR_WALLET=7MHVeATTdEZF2b4fNiRXNguC8Vhe69zWtqqtzm5TAZ3y
export CLUSTER_NAME=numinous
export INSTANCE_TYPE=tdx.medium
# New: specify node
export PHALA_NODE_NAME=prod5

bash scripts/setup_phala_cluster.sh
```

**Step 2: Verify CVM_BASE_DOMAIN is correct on the new CVM**

After deploy, use the debug endpoint:
```bash
curl -sk "https://<new_app_id>-9010.dstack-pha-prod5.phala.network/debug/exec?cmd=env" | python3 -c "
import json, sys
data = json.load(sys.stdin)
for line in data['stdout'].strip().split('\n'):
    if 'CVM_BASE_DOMAIN' in line or 'AWS' in line:
        print(line)
"
```
Expected: `CVM_BASE_DOMAIN=dstack-pha-prod5.phala.network` and AWS creds present.

**Step 3: Update orchestrator on staging to point to new CVM**

Restart the model-orchestrator container on staging so it rediscovers the new CVM.

**Step 4: Upload a model and verify gRPC connection succeeds**

Watch orchestrator logs for the model going from BUILDING → RUNNING without CONNECTION_FAILED.
