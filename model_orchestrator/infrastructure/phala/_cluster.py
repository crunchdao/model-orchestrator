"""
Phala CVM Cluster Manager.

Discovers and manages CVMs via the Phala Cloud API. Provides head-based
routing for new model builds and task-based routing for status polling,
start, and stop operations.

Source of truth:
    - What CVMs exist?       → Phala Cloud API (GET /cvms, filter by name prefix)
    - What mode is a CVM?    → CVM itself (GET /health → mode)
    - Has CVM capacity?      → CVM itself (GET /capacity → accepting_new_models)
    - Which models on which? → CVM itself (GET /running_models)

The orchestrator keeps no persistent CVM or model-mapping state.

Capacity planning:
    Each CVM decides for itself whether it can accept new models via
    its /capacity endpoint (accepting_new_models flag, controlled by
    CAPACITY_THRESHOLD). The orchestrator simply asks the head CVM
    "can you take more?" and provisions a new runner when the answer
    is no. A global max_models cap prevents unbounded cluster growth.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
import threading
import uuid

import requests

from ._client import SpawnteeAuthenticationError, SpawnteeClient, SpawnteeClientError

from ._gateway_credentials import GatewayCredentials

logger = logging.getLogger(__name__)

# Phala instance type → memory in MB.
# From `phala instance-types` (2026-02).
INSTANCE_TYPE_MEMORY_MB: dict[str, int] = {
    "tdx.small":    2048,
    "tdx.medium":   4096,
    "tdx.large":    8192,
    "tdx.xlarge":  16384,
    "tdx.2xlarge": 32768,
    "tdx.4xlarge": 65536,
    "tdx.8xlarge": 131072,
}

# Default Phala Cloud API base URL
DEFAULT_PHALA_API_URL = "https://cloud-api.phala.network"


class PhalaClusterError(Exception):
    """Error in cluster operations (discovery, provisioning, routing)."""
    pass


class CVMInfo:
    """Discovered CVM with its client and metadata."""

    def __init__(self, app_id: str, name: str, client: SpawnteeClient, mode: str = "", node_name: str = ""):
        self.app_id = app_id
        self.name = name
        self.client = client
        self.mode = mode  # "registry+runner" or "runner"
        self.node_name = node_name

    def __repr__(self):
        return f"CVMInfo(app_id={self.app_id!r}, name={self.name!r}, mode={self.mode!r})"


class PhalaCluster:
    """
    Manages a cluster of Phala CVMs for model execution.

    Discovery: queries the Phala Cloud API for CVMs matching a name prefix,
    then probes each CVM's /health endpoint to determine its mode.

    Head CVM: the CVM currently accepting new models. When it's full,
    a new runner CVM is provisioned and becomes the new head.

    Task routing: maps task_id → app_id so that status polls, start,
    and stop operations hit the correct CVM. Built on startup by scanning
    all CVMs' /running_models endpoint.
    """

    def __init__(
        self,
        cluster_name: str,
        gateway_key_path: str,
        spawntee_port: int = 9010,
        request_timeout: int = 30,
        phala_api_url: str = "",
        runner_compose_path: str = "",
        instance_type: str = "tdx.medium",
        max_models: int = 0,
        memory_per_model_mb: int = 256,
        capacity_threshold: float = 0.8,
    ):
        """
        Args:
            cluster_name: Name prefix for CVM discovery (e.g. "bird-tracker").
                         CVMs are expected to be named "{cluster_name}-registry",
                         "{cluster_name}-runner-001", etc.
            spawntee_port: Port where spawntee API listens on each CVM.
            request_timeout: HTTP timeout for spawntee API calls.
            phala_api_url: Phala Cloud API base URL.
            runner_compose_path: Path to docker-compose.phala.runner.yml for provisioning runners.
            instance_type: Phala CVM instance type for new runners (e.g. "tdx.medium").
            max_models: Global cap on total models across the cluster. 0 = unlimited.
            memory_per_model_mb: Memory budget per model in MB. Passed to runner CVMs
                as MODEL_MEMORY_LIMIT_MB env var at deploy time. The CVM uses it for
                capacity planning (max_models) and container enforcement (ulimit).
            capacity_threshold: Fraction of CVM capacity at which it reports full
                (0.0–1.0). Passed as CAPACITY_THRESHOLD env var to provisioned runners.
        """
        self.cluster_name = cluster_name
        self.spawntee_port = spawntee_port
        self.request_timeout = request_timeout
        self.phala_api_url = phala_api_url or DEFAULT_PHALA_API_URL
        self.phala_api_key = os.environ.get("PHALA_API_KEY", "")
        self.runner_compose_path = runner_compose_path

        # Load coordinator gateway credentials for spawntee API auth.
        key_path = Path(gateway_key_path)
        if not key_path.exists():
            raise PhalaClusterError(
                f"Gateway key file not found: {gateway_key_path}"
            )
        self.gateway_credentials = GatewayCredentials.from_pem(
            key_pem=key_path.read_bytes(),
        )
        logger.info("🔑 Loaded gateway credentials from %s", key_path)

        # Instance type is only used when provisioning new runner CVMs.
        self.instance_type = instance_type
        self.max_models = max_models  # 0 = unlimited
        self.memory_per_model_mb = memory_per_model_mb
        self.capacity_threshold = capacity_threshold

        if instance_type not in INSTANCE_TYPE_MEMORY_MB:
            raise PhalaClusterError(
                f"Unknown instance type '{instance_type}'. "
                f"Known types: {', '.join(sorted(INSTANCE_TYPE_MEMORY_MB))}"
            )

        logger.info(
            "📐 Capacity: instance=%s, model_memory=%dMB, capacity_threshold=%.2f, global_max=%s",
            instance_type,
            memory_per_model_mb,
            capacity_threshold,
            max_models or "unlimited",
        )

        # Lock protecting cvms, head_id, and task_client_map against
        # concurrent mutation (e.g. ensure_capacity racing with register_task).
        self._lock = threading.Lock()

        # Discovered CVMs: app_id → CVMInfo
        self.cvms: dict[str, CVMInfo] = {}

        # Head CVM: the one currently accepting new models
        self.head_id: str | None = None

        # Task routing: task_id → app_id
        self.task_client_map: dict[str, str] = {}

    # ─── Discovery ───

    def discover(self):
        """Discover CVMs from the Phala Cloud API and probe each one."""
        if not self.cluster_name or not self.phala_api_key:
            logger.warning("⚠️ No cluster_name or API key configured - no CVMs discovered")
            return

        self._discover_from_api()

        if not self.cvms:
            logger.warning("⚠️ No CVMs discovered")
            return

        # Determine head: pick a CVM that has capacity, preferring
        # runners over the registry.
        # has_capacity() retries internally; if it still fails, the error
        # propagates — we refuse to guess.
        candidates = sorted(
            self.cvms.values(),
            key=lambda c: (0 if c.mode == "runner" else 1),
        )
        for cvm in candidates:
            if cvm.client.has_capacity():
                self.head_id = cvm.app_id
                logger.info("📍 Head CVM: %s (%s)", cvm.name, cvm.app_id)
                break

        if not self.head_id:
            # All CVMs report full (but reachable). Use the registry as head;
            # ensure_capacity will provision a new runner when a build arrives.
            registry = next((c for c in self.cvms.values() if "registry" in c.mode), None)
            if not registry:
                raise PhalaClusterError(
                    "No registry CVM found in cluster — cannot operate without a registry"
                )
            self.head_id = registry.app_id
            logger.warning("⚠️ No CVM has capacity, defaulting head to registry: %s", registry.name)

        logger.info("✅ Discovered %d CVM(s), head=%s", len(self.cvms), self.head_id)

        # Push runner compose hashes to the registry so it can verify
        # re-encryption attestation. This is idempotent and ensures the
        # registry has the correct hashes even after a restart.
        # Best-effort during discover: if no runners exist yet there is
        # nothing to push, and _provision_new_runner() will approve the
        # hash before promoting the runner to head.
        try:
            self._approve_runner_hashes_on_registry()
        except (requests.RequestException, SpawnteeClientError, PhalaClusterError) as e:
            logger.warning("⚠️ Could not approve runner hashes during discover (will retry on provision): %s", e)

    def _discover_from_api(self):
        """Query Phala Cloud API for CVMs matching our cluster name prefix.

        Raises on API errors — discovery must not silently return an empty cluster.
        """
        logger.info("🔍 Discovering CVMs from Phala API (prefix=%s)...", self.cluster_name)

        headers = {"X-API-Key": self.phala_api_key}
        response = requests.get(
            f"{self.phala_api_url}/api/v1/cvms",
            headers=headers,
            timeout=15,
        )
        response.raise_for_status()
        all_cvms = response.json()

        # Filter by name prefix
        matching = [c for c in all_cvms if c.get("name", "").startswith(self.cluster_name)]
        logger.info("  Found %d CVM(s) matching prefix '%s' (out of %d total)",
                     len(matching), self.cluster_name, len(all_cvms))

        for cvm_data in matching:
            app_id = cvm_data["app_id"]
            name = cvm_data["name"]
            node_info = cvm_data.get("node_info") or {}
            node_name = node_info.get("name", "")
            status = cvm_data.get("status", "")

            if status != "running":
                logger.info("  Skipping %s (%s) - status=%s", name, app_id, status)
                continue

            if not node_name:
                raise PhalaClusterError(
                    f"CVM {name} ({app_id}) has no node_info.name in Phala API list response"
                )

            # Build URL template from app_id and node_name
            url_template = f"https://{app_id}-<model-port>.dstack-pha-{node_name}.phala.network"
            client = SpawnteeClient(
                cluster_url_template=url_template,
                spawntee_port=self.spawntee_port,
                timeout=self.request_timeout,
                gateway_credentials=self.gateway_credentials,
            )

            # Probe health to get mode
            mode = self._probe_mode(client, name)
            if mode is None:
                continue

            cvm_info = CVMInfo(
                app_id=app_id,
                name=name,
                client=client,
                mode=mode,
                node_name=node_name,
            )
            self.cvms[app_id] = cvm_info
            logger.info("  ✅ %s (%s) mode=%s", name, app_id, mode)

    def _get_compose_hash(self, app_id: str) -> str:
        """Look up a CVM's compose_hash from the Phala API.

        Raises PhalaClusterError if the hash cannot be retrieved or is missing.
        """
        headers = {"X-API-Key": self.phala_api_key}
        response = requests.get(
            f"{self.phala_api_url}/api/v1/cvms/{app_id}",
            headers=headers,
            timeout=15,
        )
        response.raise_for_status()
        cvm = response.json()
        if "compose_hash" not in cvm:
            raise PhalaClusterError(
                f"CVM {app_id} has no compose_hash in Phala API response"
            )
        return cvm["compose_hash"]

    def _get_node_name(self, app_id: str) -> str:
        """Look up a CVM's node name from the Phala API.

        Raises PhalaClusterError if the node name cannot be retrieved or is missing.
        """
        headers = {"X-API-Key": self.phala_api_key}
        response = requests.get(
            f"{self.phala_api_url}/api/v1/cvms/{app_id}",
            headers=headers,
            timeout=15,
        )
        response.raise_for_status()
        cvm = response.json()
        node_info = cvm.get("node_info") or {}
        name = node_info.get("name", "")
        if not name:
            raise PhalaClusterError(
                f"CVM {app_id} has no node_info.name in Phala API response"
            )
        return name

    def _probe_mode(self, client: SpawnteeClient, label: str) -> str | None:
        """Probe a CVM's /health endpoint to determine its mode.

        Returns None if the CVM is not a spawntee service (caller should skip it).
        Raises PhalaClusterError if it is a spawntee service but has no mode.

        health() retries internally on transient errors. If it still fails
        (auth error, retries exhausted), the exception propagates — we
        refuse to silently skip a CVM we can't reach.
        """
        health = client.health()
        if health.get("service") != "secure-spawn":
            logger.warning("  ⚠️ %s is not a spawntee service, skipping", label)
            return None
        mode = health.get("mode")
        if not mode:
            raise PhalaClusterError(
                f"CVM {label} is a spawntee service but has no 'mode' in health response"
            )
        return mode

    def _approve_runner_hashes_on_registry(self):
        """
        Collect compose hashes from all runner CVMs and push them to the registry.

        Called after discover() and after provisioning a new runner. This ensures
        the registry always knows which runner compose hashes to accept for
        re-encryption attestation — even after a registry restart.
        """
        registry = next((c for c in self.cvms.values() if "registry" in c.mode), None)
        if not registry:
            logger.debug("No registry CVM found, skipping hash approval")
            return

        runner_ids = [
            app_id for app_id, cvm in self.cvms.items()
            if cvm.mode == "runner"
        ]
        if not runner_ids:
            logger.debug("No runner CVMs found, skipping hash approval")
            return

        # Collect compose hashes from Phala API (raises on failure)
        hashes = []
        for app_id in runner_ids:
            h = self._get_compose_hash(app_id)
            hashes.append(h)
            logger.debug("  Runner %s compose_hash: %s", app_id, h[:16] + "...")

        # Deduplicate (all runners with same image + env vars get the same hash)
        unique_hashes = list(set(hashes))

        try:
            result = registry.client.approve_hashes(unique_hashes)
            logger.info(
                "🔐 Approved %d compose hash(es) on registry %s",
                result.get("approved_count", len(unique_hashes)),
                registry.name,
            )
        except (requests.RequestException, SpawnteeClientError) as e:
            logger.error("❌ Failed to approve hashes on registry: %s", e)
            raise

    # ─── Task routing ───

    def rebuild_task_map(self):
        """
        Scan all CVMs for running models and rebuild the task → CVM mapping.

        Called on startup to recover task routing after an orchestrator restart.
        """
        with self._lock:
            self._rebuild_task_map_unlocked()

    def _rebuild_task_map_unlocked(self):
        """Scan all CVMs for running models. Errors propagate after retries."""
        self.task_client_map.clear()
        for app_id, cvm in self.cvms.items():
            running = cvm.client.get_running_models()
            for model in running:
                task_id = model.get("task_id")
                if task_id:
                    self.task_client_map[task_id] = app_id
            logger.info("  📋 %s: %d running model(s)", cvm.name, len(running))

        logger.info("✅ Task map rebuilt: %d task(s) across %d CVM(s)",
                     len(self.task_client_map), len(self.cvms))

    def register_task(self, task_id: str, app_id: str):
        """Record which CVM owns a task (called after build/start)."""
        with self._lock:
            self.task_client_map[task_id] = app_id

    # ─── Client access ───

    def head_client(self) -> SpawnteeClient:
        """
        Get the head CVM's client (the one accepting new models).

        Raises PhalaClusterError if no head is set.
        """
        with self._lock:
            if not self.head_id or self.head_id not in self.cvms:
                raise PhalaClusterError("No head CVM available")
            return self.cvms[self.head_id].client

    def client_for_task(self, task_id: str) -> SpawnteeClient:
        """
        Get the client for the CVM that owns a given task.

        Raises PhalaClusterError if the task is not in the map.
        """
        with self._lock:
            app_id = self.task_client_map.get(task_id)
            if app_id and app_id in self.cvms:
                return self.cvms[app_id].client
        raise PhalaClusterError(f"Task {task_id} not found in task routing map")

    def all_clients(self) -> list[tuple[str, SpawnteeClient]]:
        """Return all (app_id, client) pairs for scanning operations."""
        with self._lock:
            return [(app_id, cvm.client) for app_id, cvm in self.cvms.items()]

    # ─── Capacity management ───

    def total_running_models(self) -> int:
        """Count models across all CVMs based on the task routing map."""
        with self._lock:
            return len(self.task_client_map)

    def head_model_count(self) -> int:
        """Count how many models are assigned to the current head CVM."""
        with self._lock:
            if not self.head_id:
                return 0
            return sum(1 for app_id in self.task_client_map.values() if app_id == self.head_id)

    def ensure_capacity(self):
        """
        Check if the head CVM has capacity. If not, find or provision one that does.

        Decision logic:
        1. Global cap: if total_running_models >= max_models, refuse.
        2. Ask the head CVM: GET /capacity → accepting_new_models.
           The CVM decides based on its own memory/disk usage and
           CAPACITY_THRESHOLD. If it says no, find another CVM or
           provision a new runner.

        Called before each build to ensure we have somewhere to put the model.

        Thread-safe: the /capacity network call is made outside the lock so
        that register_task / head_client / client_for_task are not blocked
        by the HTTP round-trip.
        """
        # ── Phase 1: snapshot head info under the lock (fast) ────────
        with self._lock:
            if not self.head_id:
                raise PhalaClusterError("No head CVM available")

            # 1. Global cap
            total = len(self.task_client_map)
            if self.max_models and total >= self.max_models:
                raise PhalaClusterError(
                    f"Global model cap reached ({total}/{self.max_models}). "
                    f"Cannot accept new models."
                )

            head = self.cvms[self.head_id]
            head_id_snapshot = self.head_id

        # ── Phase 2: ask the CVM (network call, no lock) ────────────
        accepting = head.client.has_capacity()

        # ── Phase 3: decide and optionally provision ────────────────
        # First, check under the lock whether another thread already handled it.
        with self._lock:
            if self.head_id != head_id_snapshot:
                logger.debug(
                    "↩️ Head changed (%s → %s) while querying capacity, skipping",
                    head_id_snapshot, self.head_id,
                )
                return

            if accepting:
                logger.debug(
                    "✅ Head CVM %s has capacity (global=%d/%s)",
                    head.name, total, self.max_models or "∞",
                )
                return

            # Head is full — snapshot other CVMs to scan outside the lock
            logger.info(
                "📊 Head CVM %s reports accepting_new_models=false, "
                "looking for CVM with capacity...",
                head.name,
            )
            candidates = sorted(
                [cvm for cvm in self.cvms.values() if cvm.app_id != head_id_snapshot],
                key=lambda c: (0 if c.mode == "runner" else 1),
            )

        # ── Phase 3b: scan other CVMs for capacity (no lock — network calls) ──
        new_head_id = None
        for cvm in candidates:
            try:
                if cvm.client.has_capacity():
                    new_head_id = cvm.app_id
                    logger.info(
                        "📍 Found existing CVM with capacity: %s (%s)",
                        cvm.name, cvm.app_id,
                    )
                    break
            except SpawnteeClientError as e:
                logger.debug("  ⏳ CVM %s unreachable for capacity check: %s", cvm.name, e)
                continue

        # ── Phase 3c: apply result or provision (lock for state mutation only) ──
        if new_head_id:
            with self._lock:
                # Another thread may have already changed the head; only
                # update if it's still the same stale head we started with.
                if self.head_id == head_id_snapshot:
                    self.head_id = new_head_id
                    logger.info(
                        "📍 Switched head to existing CVM with capacity: %s",
                        new_head_id,
                    )
            return

        logger.info("  No existing CVM has capacity, provisioning new runner...")
        # Provisioning is a long operation (subprocess + health checks).
        # It mutates self.cvms and self.head_id at the very end, so we
        # hold the lock only for the final state update inside _provision_new_runner.
        self._provision_new_runner()

    def _provision_new_runner(self):
        """
        Provision a new runner CVM via the phala CLI.

        Creates a new CVM with MODE=runner, names it
        "{cluster_name}-runner-{N}", and makes it the new head.
        """
        import json
        import subprocess
        import time

        if not self.phala_api_key:
            raise PhalaClusterError(
                "Cannot provision new CVM: PHALA_API_KEY not set"
            )

        if not self.runner_compose_path:
            raise PhalaClusterError(
                "Cannot provision new CVM: runner_compose_path not configured"
            )

        # Generate a unique runner name using a short UUID to avoid collisions
        # with CVMs that may still exist on the platform but are no longer tracked
        short_id = uuid.uuid4().hex[:8]
        cvm_name = f"{self.cluster_name}-runner-{short_id}"

        logger.info("🆕 Provisioning CVM: %s", cvm_name)

        # Get registry URL for the runner's REGISTRY_URL env var
        registry_cvm = next(
            (c for c in self.cvms.values() if "registry" in c.mode), None
        )
        if not registry_cvm:
            raise PhalaClusterError("No registry CVM found - cannot set REGISTRY_URL for runner")
        if not registry_cvm.node_name:
            raise PhalaClusterError(
                f"Registry CVM {registry_cvm.name} has no node_name - cannot construct REGISTRY_URL"
            )

        registry_url = f"https://{registry_cvm.app_id}-{self.spawntee_port}.dstack-pha-{registry_cvm.node_name}.phala.network"

        # We shell out to the `phala` CLI instead of calling the Phala Cloud API
        # directly because the CLI handles x25519+AES-GCM encryption of environment
        # variables before sending them to the CVM. Re-implementing that crypto
        # (ECDH key agreement, nonce derivation, authenticated encryption) is
        # non-trivial and fragile — the CLI already does it correctly.
        coordinator_wallet = os.environ.get("GATEWAY_AUTH_COORDINATOR_WALLET", "")

        cmd = [
            "phala", "deploy",
            "--name", cvm_name,
            "--instance-type", self.instance_type,
            "--compose", self.runner_compose_path,
            "-e", f"REGISTRY_URL={registry_url}",
            "-e", f"CAPACITY_THRESHOLD={self.capacity_threshold}",
            "-e", f"MODEL_MEMORY_LIMIT_MB={self.memory_per_model_mb}",
        ]
        if coordinator_wallet:
            cmd.extend(["-e", f"GATEWAY_AUTH_COORDINATOR_WALLET={coordinator_wallet}"])
        cmd.extend(["--json", "--wait"])

        logger.info("  Running: %s", " ".join(cmd))

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=300,  # 5 minute timeout for deploy + wait
                env={**os.environ, "PHALA_API_KEY": self.phala_api_key, "PHALA_CLOUD_API_KEY": self.phala_api_key},
            )
        except subprocess.TimeoutExpired:
            raise PhalaClusterError(f"phala deploy timed out for {cvm_name}")

        if result.returncode != 0:
            logger.error("  phala deploy stderr: %s", result.stderr)
            raise PhalaClusterError(
                f"phala deploy failed (rc={result.returncode}): {result.stderr}"
            )

        # Parse JSON output to get app_id and node info.
        # phala CLI may prefix JSON with text like "Provisioning CVM...",
        # so we extract the first JSON object from the output.
        deploy_result = {}
        stdout = result.stdout.strip()
        json_start = stdout.find("{")
        if json_start >= 0:
            try:
                deploy_result, _ = json.JSONDecoder().raw_decode(stdout[json_start:])
            except json.JSONDecodeError:
                logger.warning("  Could not parse phala deploy JSON: %s", stdout[:500])
        else:
            logger.warning("  No JSON found in phala deploy output: %s", stdout[:500])

        new_app_id = deploy_result.get("app_id", "")

        if not new_app_id:
            raise PhalaClusterError(
                f"CVM {cvm_name} deployed but could not determine app_id from output"
            )

        # Get node_name from API (deploy output doesn't include it)
        node_name = self._get_node_name(new_app_id)

        logger.info("  ✅ CVM created: %s (app_id=%s)", cvm_name, new_app_id)

        # Build client and wait for healthy
        url_template = f"https://{new_app_id}-<model-port>.dstack-pha-{node_name}.phala.network"
        new_client = SpawnteeClient(
            cluster_url_template=url_template,
            spawntee_port=self.spawntee_port,
            timeout=self.request_timeout,
            gateway_credentials=self.gateway_credentials,
        )

        # Wait for the CVM to be fully ready in two stages:
        #   1. /health → "healthy"  (FastAPI is up)
        #   2. /capacity → accepting_new_models: true  (Docker, nginx, full stack ready)
        # Without stage 2, builds routed to the new head can fail because
        # Docker-in-Docker hasn't finished initializing yet.
        #
        # Uses wall-clock time (not loop-count) because each HTTP request
        # can take 30+ seconds with internal retries and backoff.
        max_wait = 180  # 3 minutes total for both stages
        interval = 10
        deadline = time.monotonic() + max_wait

        # Stage 1: wait for healthy
        ready = False
        while time.monotonic() < deadline:
            try:
                health = new_client.health()
                if health.get("status") == "healthy":
                    elapsed = max_wait - (deadline - time.monotonic())
                    logger.info("  ✅ CVM %s is healthy (took %ds)", cvm_name, int(elapsed))
                    break
            except SpawnteeClientError:
                pass
            time.sleep(interval)
        else:
            self._cleanup_failed_runner(new_app_id, cvm_name, "never became healthy")
            return

        # Stage 2: wait for capacity (full readiness)
        while time.monotonic() < deadline:
            try:
                cap = new_client.capacity()
                if cap.get("accepting_new_models"):
                    elapsed = max_wait - (deadline - time.monotonic())
                    logger.info(
                        "  ✅ CVM %s is ready (accepting models, took %ds total)",
                        cvm_name, int(elapsed),
                    )
                    ready = True
                    break
            except SpawnteeAuthenticationError:
                raise
            except SpawnteeClientError as e:
                logger.debug("  ⏳ CVM %s capacity not ready yet: %s", cvm_name, e)
            time.sleep(interval)
        else:
            self._cleanup_failed_runner(new_app_id, cvm_name, "healthy but not accepting models")
            return

        if not ready:
            return

        # Add to cluster and make it the new head (under lock for state mutation)
        cvm_info = CVMInfo(
            app_id=new_app_id,
            name=cvm_name,
            client=new_client,
            mode="runner",
            node_name=node_name,
        )

        with self._lock:
            self.cvms[new_app_id] = cvm_info

        # Push the new runner's compose hash to the registry so it can
        # verify re-encryption attestation for this runner.
        # Must succeed before promoting to head — otherwise builds will
        # be routed here but re-encryption will fail.
        try:
            self._approve_runner_hashes_on_registry()
        except (requests.RequestException, SpawnteeClientError, PhalaClusterError):
            self._cleanup_failed_runner(new_app_id, cvm_name, "could not approve compose hash on registry")
            return

        with self._lock:
            self.head_id = new_app_id
        logger.info("  📍 New head CVM: %s (%s)", cvm_name, new_app_id)

    def _cleanup_failed_runner(self, app_id: str, name: str, reason: str):
        """Clean up a runner CVM that failed to become ready."""
        import subprocess

        logger.error(
            "  ❌ CVM %s (%s) failed: %s. Deleting and falling back to existing head.",
            name, app_id, reason,
        )

        # Remove from cluster state (it was never added, but be safe)
        with self._lock:
            self.cvms.pop(app_id, None)
            if self.head_id == app_id:
                # Revert to the registry as head
                registry = next((c for c in self.cvms.values() if "registry" in c.mode), None)
                if registry:
                    self.head_id = registry.app_id
                    logger.info("  📍 Reverted head to registry: %s", registry.name)
                elif self.cvms:
                    self.head_id = next(iter(self.cvms))
                else:
                    self.head_id = None

        # Best-effort delete from Phala Cloud
        try:
            subprocess.run(
                ["phala", "cvms", "delete", app_id, "--force"],
                capture_output=True, text=True, timeout=30,
                env={**os.environ, "PHALA_API_KEY": self.phala_api_key,
                     "PHALA_CLOUD_API_KEY": self.phala_api_key},
            )
            logger.info("  🗑️ Deleted failed CVM %s from Phala Cloud", name)
        except Exception as e:
            logger.warning("  ⚠️ Could not delete failed CVM %s: %s", name, e)
