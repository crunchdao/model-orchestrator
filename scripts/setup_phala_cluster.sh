#!/usr/bin/env bash
#
# setup_cluster.sh — One-script setup for the Crunch TEE cluster.
#
# Run from the model-orchestrator-new repo root.
# Assumes cruncher_phala is a sibling directory (../cruncher_phala).
#
# Prerequisites:
#   - Auth middleware must already be committed in cruncher_phala (spawntee/src/main.py)
#   - Token support must already be committed in orchestrator (_client.py, _cluster.py)
#   - Docker image with auth support must be pushed to Docker Hub
#
# What this script does:
#   1. Verify environment (repos, tools, code changes in place)
#   2. Collect configuration (AWS creds, Phala API key, etc.)
#   3. Generate SPAWNTEE_API_TOKEN + write env files
#   4. Build & push Docker image (optional)
#   5. Deploy registry CVM
#   6. Deploy runner CVM
#   7. Configure attestation (APPROVED_COMPOSE_HASH)
#   8. Write orchestrator .env
#   9. Verify & summary
#
set -euo pipefail

# ── Paths ────────────────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ORCH_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PHALA_ROOT="$(cd "$ORCH_ROOT/../cruncher_phala" 2>/dev/null && pwd)" || PHALA_ROOT=""

# ── Colors ───────────────────────────────────────────────────

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[0;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'

info()  { echo -e "${GREEN}✅ $1${NC}"; }
warn()  { echo -e "${YELLOW}⚠️  $1${NC}"; }
err()   { echo -e "${RED}❌ $1${NC}"; }
phase() { echo -e "\n${BOLD}${CYAN}═══ $1 ═══${NC}\n"; }
ask() {
    echo -ne "${BOLD}$1${NC}" >&2
    local v
    read -r v
    echo "$v"
}

confirm() {
    local reply
    read -rp "$(echo -e "${BOLD}$1 [y/N] ${NC}")" reply
    [[ "$reply" =~ ^[Yy] ]]
}

# ── Phase 1: Verify environment ──────────────────────────────

phase "Phase 1: Verify environment"

# Check sibling repo
if [[ -z "$PHALA_ROOT" || ! -f "$PHALA_ROOT/spawntee/src/main.py" ]]; then
    err "Cannot find cruncher_phala repo at ../cruncher_phala"
    echo "    Expected directory layout:"
    echo "      some-parent/"
    echo "        ├── model-orchestrator-new/   ← you are here"
    echo "        └── cruncher_phala/"
    exit 1
fi
info "Found cruncher_phala at $PHALA_ROOT"
info "Found orchestrator at $ORCH_ROOT"

# Check tools
for cmd in phala python3 openssl curl; do
    if ! command -v "$cmd" &>/dev/null; then
        err "Required tool not found: $cmd"
        exit 1
    fi
done
info "All required tools found (phala, python3, openssl, curl)"

# Check that auth code changes are already committed
MISSING=""
if ! grep -q "SPAWNTEE_API_TOKEN" "$PHALA_ROOT/spawntee/src/main.py"; then
    MISSING="$MISSING\n  - cruncher_phala/spawntee/src/main.py (auth middleware)"
fi
if ! grep -q "SPAWNTEE_API_TOKEN" "$PHALA_ROOT/spawntee/docker-compose.phala.debug.yml"; then
    MISSING="$MISSING\n  - cruncher_phala/spawntee/docker-compose.phala.debug.yml (env var)"
fi
if ! grep -q "SPAWNTEE_API_TOKEN" "$PHALA_ROOT/spawntee/docker-compose.phala.runner.yml"; then
    MISSING="$MISSING\n  - cruncher_phala/spawntee/docker-compose.phala.runner.yml (env var)"
fi
if ! grep -q "api_token" "$ORCH_ROOT/model_orchestrator/infrastructure/phala/_client.py"; then
    MISSING="$MISSING\n  - model-orchestrator-new/_client.py (api_token parameter)"
fi
if ! grep -q "spawntee_api_token" "$ORCH_ROOT/model_orchestrator/infrastructure/phala/_cluster.py"; then
    MISSING="$MISSING\n  - model-orchestrator-new/_cluster.py (spawntee_api_token)"
fi

if [[ -n "$MISSING" ]]; then
    err "Auth code changes are missing from the following files:"
    echo -e "$MISSING"
    echo ""
    echo "These code changes must be committed before running this script."
    echo "See docs/api-security-plan.md for the required changes."
    exit 1
fi
info "Auth code changes verified in both repos"

# ── Phase 2: Collect configuration ───────────────────────────

phase "Phase 2: Collect configuration"

echo "We need a few values to set up the cluster."
echo "Press Enter to accept defaults shown in [brackets]."
echo ""

# Load .env.setup if it exists (allows re-runs without re-entering values)
SETUP_ENV="$ORCH_ROOT/.env.setup"
if [[ -f "$SETUP_ENV" ]]; then
    info "Loading saved configuration from $SETUP_ENV"
    source "$SETUP_ENV"
    echo ""
fi

# AWS credentials (for registry CVM S3 access)
if [[ -z "${AWS_ACCESS_KEY_ID:-}" ]]; then
    AWS_ACCESS_KEY_ID=$(ask "AWS_ACCESS_KEY_ID: ")
    if [[ -z "$AWS_ACCESS_KEY_ID" ]]; then
        err "AWS_ACCESS_KEY_ID is required (registry CVM needs S3 access)"
        exit 1
    fi
else
    info "AWS_ACCESS_KEY_ID is set (from env)"
fi

if [[ -z "${AWS_SECRET_ACCESS_KEY:-}" ]]; then
    AWS_SECRET_ACCESS_KEY=$(ask "AWS_SECRET_ACCESS_KEY: ")
    if [[ -z "$AWS_SECRET_ACCESS_KEY" ]]; then
        err "AWS_SECRET_ACCESS_KEY is required"
        exit 1
    fi
else
    info "AWS_SECRET_ACCESS_KEY is set (from env)"
fi

default_region="eu-west-1"
if [[ -z "${AWS_REGION:-}" ]]; then
    AWS_REGION=$(ask "AWS_REGION [$default_region]: ")
    AWS_REGION="${AWS_REGION:-$default_region}"
else
    info "AWS_REGION is set: $AWS_REGION"
fi

echo ""

# Phala API key
if [[ -z "${PHALA_API_KEY:-}" ]]; then
    PHALA_API_KEY=$(ask "PHALA_API_KEY (from https://cloud.phala.network): ")
    if [[ -z "$PHALA_API_KEY" ]]; then
        err "PHALA_API_KEY is required. Run 'phala login' or get it from the dashboard."
        exit 1
    fi
else
    info "PHALA_API_KEY is set (from env)"
fi

echo ""

default_cluster="crunch-tee"
if [[ -z "${CLUSTER_NAME:-}" ]]; then
    CLUSTER_NAME=$(ask "Cluster name prefix [$default_cluster]: ")
    CLUSTER_NAME="${CLUSTER_NAME:-$default_cluster}"
else
    info "CLUSTER_NAME is set: $CLUSTER_NAME"
fi

default_instance="tdx.medium"
if [[ -z "${INSTANCE_TYPE:-}" ]]; then
    INSTANCE_TYPE=$(ask "Instance type [$default_instance]: ")
    INSTANCE_TYPE="${INSTANCE_TYPE:-$default_instance}"
else
    info "INSTANCE_TYPE is set: $INSTANCE_TYPE"
fi

echo ""

# Generate API token (reuse if already set)
if [[ -z "${SPAWNTEE_API_TOKEN:-}" ]]; then
    SPAWNTEE_API_TOKEN=$(openssl rand -hex 32)
    info "Generated SPAWNTEE_API_TOKEN: ${SPAWNTEE_API_TOKEN:0:8}...${SPAWNTEE_API_TOKEN:56}"
else
    info "SPAWNTEE_API_TOKEN is set (from env): ${SPAWNTEE_API_TOKEN:0:8}..."
fi

echo ""
echo -e "${BOLD}Configuration summary:${NC}"
echo "  AWS_REGION:          $AWS_REGION"
echo "  CLUSTER_NAME:        $CLUSTER_NAME"
echo "  INSTANCE_TYPE:       $INSTANCE_TYPE"
echo "  SPAWNTEE_API_TOKEN:  ${SPAWNTEE_API_TOKEN:0:8}..."
echo ""

if ! confirm "Proceed with this configuration?"; then
    echo "Aborted."
    exit 0
fi

# Save configuration for re-runs
cat > "$SETUP_ENV" <<EOF
AWS_ACCESS_KEY_ID=$AWS_ACCESS_KEY_ID
AWS_SECRET_ACCESS_KEY=$AWS_SECRET_ACCESS_KEY
AWS_REGION=$AWS_REGION
PHALA_API_KEY=$PHALA_API_KEY
CLUSTER_NAME=$CLUSTER_NAME
INSTANCE_TYPE=$INSTANCE_TYPE
SPAWNTEE_API_TOKEN=$SPAWNTEE_API_TOKEN
EOF
chmod 600 "$SETUP_ENV"
info "Configuration saved to $SETUP_ENV (for re-runs)"

# ── Phase 3: Write env files ─────────────────────────────────

phase "Phase 3: Write env files"

# CVM secrets (.env.secret)
ENV_SECRET="$PHALA_ROOT/spawntee/.env.secret"
if [[ -f "$ENV_SECRET" ]]; then
    warn ".env.secret already exists. Backing up to .env.secret.backup"
    cp "$ENV_SECRET" "$ENV_SECRET.backup"
fi

cat > "$ENV_SECRET" <<EOF
AWS_ACCESS_KEY_ID=$AWS_ACCESS_KEY_ID
AWS_SECRET_ACCESS_KEY=$AWS_SECRET_ACCESS_KEY
AWS_REGION=$AWS_REGION
SPAWNTEE_API_TOKEN=$SPAWNTEE_API_TOKEN
EOF
info "Wrote $ENV_SECRET"

# Verify gitignore
if grep -q '\.env\.secret' "$PHALA_ROOT/.gitignore" 2>/dev/null; then
    info ".env.secret is in .gitignore"
else
    err ".env.secret is NOT in .gitignore — refusing to continue"
    exit 1
fi

# ── Phase 4: Build & push Docker image ───────────────────────

phase "Phase 4: Build & push Docker image"

IMAGE_TAG=$(cd "$PHALA_ROOT" && grep 'VERSION = ' spawntee/src/main.py | head -1 | cut -d'"' -f2)
echo "Current image version: $IMAGE_TAG"
echo ""

if ! confirm "Build and push borisndocker/crunch-tee-spawn:$IMAGE_TAG now?"; then
    warn "Skipping build & push. Make sure the image is already on Docker Hub."
else
    for cmd in docker make; do
        if ! command -v "$cmd" &>/dev/null; then
            err "Required tool not found: $cmd"; exit 1
        fi
    done
    if ! docker info &>/dev/null; then
        err "Docker is not running. Start Docker and try again."; exit 1
    fi
    (cd "$PHALA_ROOT" && make push)
    info "Docker image built and pushed"
fi

# ── Phase 5: Deploy registry CVM ─────────────────────────────

phase "Phase 5: Deploy registry CVM"

REGISTRY_NAME="${CLUSTER_NAME}-registry"

# Check for existing CVMs
echo "Checking for existing CVMs with prefix '$CLUSTER_NAME'..."
EXISTING_CVMS=$(phala cvms list --json --api-key "$PHALA_API_KEY" 2>/dev/null \
    | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    cvms = data.get('items', data) if isinstance(data, dict) else data
    matches = [c for c in cvms if c.get('cvmName','').startswith('$CLUSTER_NAME') or c.get('name','').startswith('$CLUSTER_NAME')]
    for c in matches:
        name = c.get('cvmName') or c.get('name', '?')
        app_id = c.get('appId') or c.get('app_id', '?')
        status = c.get('status', '?')
        print(f'  {name}  app_id={app_id}  status={status}')
    if not matches:
        print('  (none)')
except: print('  (could not parse)')
" 2>/dev/null) || EXISTING_CVMS="  (could not query)"

echo "$EXISTING_CVMS"
if [[ "$EXISTING_CVMS" != *"(none)"* && "$EXISTING_CVMS" != *"could not"* ]]; then
    warn "Existing CVMs found. The setup will deploy NEW CVMs."
    if ! confirm "Continue?"; then
        echo "Aborted. Delete existing CVMs first with: phala cvms delete <app_id>"
        exit 0
    fi
fi

# Check if registry CVM already exists
REGISTRY_APP_ID=$(phala cvms list --json --api-key "$PHALA_API_KEY" 2>/dev/null \
    | python3 -c "
import sys, json
data = json.load(sys.stdin)
cvms = data.get('items', data) if isinstance(data, dict) else data
for c in cvms:
    name = c.get('cvmName') or c.get('name', '')
    if name == '$REGISTRY_NAME' and c.get('status') == 'running':
        print(c.get('appId') or c.get('app_id', '')); break
" 2>/dev/null) || REGISTRY_APP_ID=""

if [[ -n "$REGISTRY_APP_ID" ]]; then
    info "Registry CVM already exists: app_id=$REGISTRY_APP_ID (reusing)"
else
    echo ""
    echo "Deploying registry CVM: $REGISTRY_NAME"
    echo "  Compose: spawntee/docker-compose.phala.debug.yml"
    echo "  Instance: $INSTANCE_TYPE"
    echo ""

    REGISTRY_DEPLOY_OUT=$(phala deploy \
        --name "$REGISTRY_NAME" \
        --instance-type "$INSTANCE_TYPE" \
        --compose "$PHALA_ROOT/spawntee/docker-compose.phala.debug.yml" \
        -e "AWS_ACCESS_KEY_ID=$AWS_ACCESS_KEY_ID" \
        -e "AWS_SECRET_ACCESS_KEY=$AWS_SECRET_ACCESS_KEY" \
        -e "AWS_REGION=$AWS_REGION" \
        -e "SPAWNTEE_API_TOKEN=$SPAWNTEE_API_TOKEN" \
        --api-key "$PHALA_API_KEY" \
        --json \
        --wait \
        2>&1) || {
        err "Registry CVM deploy failed"
        echo "$REGISTRY_DEPLOY_OUT"
        exit 1
    }

    # Parse app_id from deploy output
    REGISTRY_APP_ID=$(echo "$REGISTRY_DEPLOY_OUT" | python3 -c "
import sys, json
text = sys.stdin.read()
idx = text.find('{')
if idx >= 0:
    obj = json.loads(text[idx:text.rfind('}')+1])
    print(obj.get('app_id', ''))
" 2>/dev/null)

    if [[ -z "$REGISTRY_APP_ID" ]]; then
        warn "Could not parse app_id from deploy output. Searching by name..."
        REGISTRY_APP_ID=$(phala cvms list --json --api-key "$PHALA_API_KEY" 2>/dev/null \
            | python3 -c "
import sys, json
data = json.load(sys.stdin)
cvms = data.get('items', data) if isinstance(data, dict) else data
for c in cvms:
    name = c.get('cvmName') or c.get('name', '')
    if name == '$REGISTRY_NAME' and c.get('status') == 'running':
        print(c.get('appId') or c.get('app_id', '')); break
" 2>/dev/null)
    fi

    if [[ -z "$REGISTRY_APP_ID" ]]; then
        err "Could not determine registry CVM app_id. Check 'phala cvms list'."
        exit 1
    fi
    info "Registry CVM deployed: app_id=$REGISTRY_APP_ID"
fi

# Get node_name for URL construction
REGISTRY_NODE=$(phala cvms get "$REGISTRY_APP_ID" --json --api-key "$PHALA_API_KEY" 2>/dev/null \
    | python3 -c "
import sys, json
data = json.load(sys.stdin)
ni = data.get('node_info') or data.get('node') or {}
print(ni.get('name', ''))
" 2>/dev/null) || REGISTRY_NODE=""

if [[ -z "$REGISTRY_NODE" ]]; then
    warn "Could not get node_name. Using default domain."
    REGISTRY_NODE="prod10"
fi

REGISTRY_URL="https://${REGISTRY_APP_ID}-9010.dstack-pha-${REGISTRY_NODE}.phala.network"

# Wait for healthy
echo "Waiting for registry to become healthy at $REGISTRY_URL ..."
HEALTHY=false
for i in $(seq 1 30); do
    if curl -sf "$REGISTRY_URL/health" >/dev/null 2>&1; then
        HEALTHY=true
        break
    fi
    echo "  attempt $i/30..."
    sleep 10
done

if [[ "$HEALTHY" != true ]]; then
    err "Registry CVM did not become healthy within 5 minutes."
    echo "  Check: $REGISTRY_URL/health"
    echo "  Logs:  phala cvms logs $REGISTRY_APP_ID"
    exit 1
fi
info "Registry CVM is healthy: $REGISTRY_URL"

# ── Phase 6: Get runner compose_hash ──────────────────────────
#
# The registry needs to know the runner's compose_hash for attestation.
# Phala computes this hash server-side, so we must deploy a temporary
# runner to discover it. We delete it immediately after.

phase "Phase 6: Get runner compose_hash (temporary deploy)"

TEMP_RUNNER_NAME="${CLUSTER_NAME}-temp-runner"

echo "Deploying temporary runner CVM to discover compose_hash..."
echo "  This runner will be deleted after we grab the hash."
echo ""

RUNNER_DEPLOY_OUT=$(phala deploy \
    --name "$TEMP_RUNNER_NAME" \
    --instance-type "$INSTANCE_TYPE" \
    --compose "$PHALA_ROOT/spawntee/docker-compose.phala.runner.yml" \
    -e "REGISTRY_URL=$REGISTRY_URL" \
    -e "SPAWNTEE_API_TOKEN=$SPAWNTEE_API_TOKEN" \
    --api-key "$PHALA_API_KEY" \
    --json \
    --wait \
    2>&1) || {
    err "Temporary runner deploy failed"
    echo "$RUNNER_DEPLOY_OUT"
    exit 1
}

# Parse app_id
TEMP_RUNNER_APP_ID=$(echo "$RUNNER_DEPLOY_OUT" | python3 -c "
import sys, json
text = sys.stdin.read()
idx = text.find('{')
if idx >= 0:
    obj = json.loads(text[idx:text.rfind('}')+1])
    print(obj.get('app_id', ''))
" 2>/dev/null)

if [[ -z "$TEMP_RUNNER_APP_ID" ]]; then
    TEMP_RUNNER_APP_ID=$(phala cvms list --json --api-key "$PHALA_API_KEY" 2>/dev/null \
        | python3 -c "
import sys, json
data = json.load(sys.stdin)
cvms = data.get('items', data) if isinstance(data, dict) else data
for c in cvms:
    name = c.get('cvmName') or c.get('name', '')
    if name == '$TEMP_RUNNER_NAME':
        print(c.get('appId') or c.get('app_id', '')); break
" 2>/dev/null)
fi

if [[ -z "$TEMP_RUNNER_APP_ID" ]]; then
    err "Could not determine temporary runner app_id."
    exit 1
fi
info "Temporary runner deployed: app_id=$TEMP_RUNNER_APP_ID"

# Get compose_hash
echo "Getting compose_hash from Phala API..."

COMPOSE_HASH=$(phala cvms get "$TEMP_RUNNER_APP_ID" --json --api-key "$PHALA_API_KEY" 2>/dev/null \
    | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    print(data.get('compose_hash', ''))
except Exception:
    pass
" 2>/dev/null) || COMPOSE_HASH=""

if [[ -z "$COMPOSE_HASH" ]]; then
    COMPOSE_HASH=$(phala cvms list --json --api-key "$PHALA_API_KEY" 2>/dev/null \
        | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    cvms = data.get('items', data) if isinstance(data, dict) else data
    for c in cvms:
        app_id = c.get('appId') or c.get('app_id', '')
        if app_id == '$TEMP_RUNNER_APP_ID':
            print(c.get('compose_hash') or c.get('composeHash', '')); break
except Exception:
    pass
" 2>/dev/null) || COMPOSE_HASH=""
fi

if [[ -n "$COMPOSE_HASH" ]]; then
    info "Runner compose_hash: $COMPOSE_HASH"
else
    err "Could not get compose_hash. You must set APPROVED_COMPOSE_HASH manually later."
fi

# Delete temporary runner
echo "Deleting temporary runner..."
phala cvms delete "$TEMP_RUNNER_APP_ID" --api-key "$PHALA_API_KEY" 2>&1 || {
    warn "Could not delete temporary runner $TEMP_RUNNER_APP_ID. Delete it manually:"
    echo "  phala cvms delete $TEMP_RUNNER_APP_ID"
}
info "Temporary runner deleted"

# ── Phase 7: Configure attestation ───────────────────────────

phase "Phase 7: Configure attestation (APPROVED_COMPOSE_HASH)"

if [[ -z "$COMPOSE_HASH" ]]; then
    err "No compose_hash available. Skipping attestation configuration."
    echo "  You must set it manually later."
else
    echo "Upgrading registry CVM with APPROVED_COMPOSE_HASH..."
    # Note: --wait is omitted because the Phala CLI has a UUID validation bug
    # when polling upgrade status. We poll the health endpoint manually instead.
    phala deploy \
        --cvm-id "$REGISTRY_APP_ID" \
        --compose "$PHALA_ROOT/spawntee/docker-compose.phala.debug.yml" \
        -e "AWS_ACCESS_KEY_ID=$AWS_ACCESS_KEY_ID" \
        -e "AWS_SECRET_ACCESS_KEY=$AWS_SECRET_ACCESS_KEY" \
        -e "AWS_REGION=$AWS_REGION" \
        -e "SPAWNTEE_API_TOKEN=$SPAWNTEE_API_TOKEN" \
        -e "APPROVED_COMPOSE_HASH=$COMPOSE_HASH" \
        --api-key "$PHALA_API_KEY" \
        2>&1 || {
        err "Failed to upgrade registry with APPROVED_COMPOSE_HASH"
        echo "  Do it manually:"
        echo "  phala deploy --cvm-id $REGISTRY_APP_ID -e APPROVED_COMPOSE_HASH=$COMPOSE_HASH ..."
    }

    # Wait for registry to come back
    echo "Waiting for registry to restart..."
    sleep 15
    UPGRADE_HEALTHY=false
    for i in $(seq 1 20); do
        if curl -sf "$REGISTRY_URL/health" >/dev/null 2>&1; then
            UPGRADE_HEALTHY=true
            info "Registry is back up with APPROVED_COMPOSE_HASH set"
            break
        fi
        echo "  attempt $i/20..."
        sleep 10
    done
    if [[ "$UPGRADE_HEALTHY" != true ]]; then
        warn "Registry did not become healthy after upgrade. Check manually: $REGISTRY_URL/health"
    fi
fi

# ── Phase 8: Write orchestrator .env ──────────────────────────

phase "Phase 8: Write orchestrator .env"

ORCH_ENV="$ORCH_ROOT/.env"
if [[ -f "$ORCH_ENV" ]]; then
    warn ".env already exists. Backing up to .env.backup"
    cp "$ORCH_ENV" "$ORCH_ENV.backup"
fi

cat > "$ORCH_ENV" <<EOF
# Generated by setup_cluster.sh on $(date -Iseconds)
# Crunch TEE cluster configuration

PHALA_API_KEY=$PHALA_API_KEY
SPAWNTEE_API_TOKEN=$SPAWNTEE_API_TOKEN
EOF
info "Wrote orchestrator .env"

# ── Phase 9: Verify & summary ────────────────────────────────

phase "Phase 9: Verify"

echo "Testing registry endpoints..."

# Health (no auth)
REGISTRY_HEALTH=$(curl -sf "$REGISTRY_URL/health" 2>/dev/null) || REGISTRY_HEALTH="UNREACHABLE"
echo "  Registry /health: $REGISTRY_HEALTH"

# Auth required (should fail without token)
NOAUTH_CODE=$(curl -s -o /dev/null -w "%{http_code}" "$REGISTRY_URL/running_models" 2>/dev/null) || NOAUTH_CODE="000"
AUTH_CODE=$(curl -s -o /dev/null -w "%{http_code}" -H "Authorization: Bearer $SPAWNTEE_API_TOKEN" "$REGISTRY_URL/running_models" 2>/dev/null) || AUTH_CODE="000"

if [[ "$NOAUTH_CODE" == "401" ]]; then
    info "Auth enforcement works: /running_models without token → 401"
else
    warn "/running_models without token returned $NOAUTH_CODE (expected 401). CVM may still be restarting."
fi

if [[ "$AUTH_CODE" == "200" ]]; then
    info "Auth works: /running_models with token → 200"
else
    warn "/running_models with token returned $AUTH_CODE (expected 200)"
fi

# ── Final summary ─────────────────────────────────────────────

phase "Setup complete!"

cat <<EOF
${BOLD}Registry CVM:${NC}
  Name:          $REGISTRY_NAME
  App ID:        $REGISTRY_APP_ID
  URL:           $REGISTRY_URL
  Mode:          registry+runner (serves keys, runs models)
  Compose hash:  ${COMPOSE_HASH:-UNKNOWN} (approved for future runners)

${BOLD}Secrets:${NC}
  SPAWNTEE_API_TOKEN:  ${SPAWNTEE_API_TOKEN:0:8}...${SPAWNTEE_API_TOKEN:56}
  Stored in:
    - $PHALA_ROOT/spawntee/.env.secret  (CVM deploys)
    - $ORCH_ROOT/.env                   (orchestrator)

${BOLD}Orchestrator config:${NC}
  Set these in your orchestrator YAML config:

    runner:
      type: phala
      cluster_name: "$CLUSTER_NAME"
      instance_type: "$INSTANCE_TYPE"

${BOLD}How it works:${NC}
  The registry CVM handles everything initially (keys + model execution).
  When it runs out of capacity, the orchestrator auto-provisions runner
  CVMs using the approved compose_hash for attestation.

${BOLD}Next steps:${NC}
  1. Start the orchestrator:  cd $ORCH_ROOT && export \$(cat .env | xargs) && poetry run model-orchestrator
  2. The orchestrator will discover the registry CVM by name prefix "$CLUSTER_NAME"
EOF
