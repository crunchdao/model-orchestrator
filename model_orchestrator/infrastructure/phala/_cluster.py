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
    max_models_per_cvm is calculated from the instance type's memory, a
    system overhead reservation, and the configured memory_per_model_mb.
    A new CVM is provisioned when the head reaches
    (max_models_per_cvm * provision_factor) models, OR when the CVM itself
    reports accepting_new_models=false (CAPACITY_THRESHOLD safety net).
    A global max_models cap prevents unbounded cluster growth.
"""

import math
import os
import logging

import requests

from ._client import SpawnteeClient, SpawnteeClientError

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

# Memory reserved for the OS, Docker daemon, spawn service, etc.
SYSTEM_OVERHEAD_MB = 500

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
        spawntee_port: int = 9010,
        request_timeout: int = 30,
        phala_api_url: str = "",
        runner_compose_path: str = "",
        fallback_urls: list[str] | None = None,
        instance_type: str = "tdx.medium",
        memory_per_model_mb: int = 1024,
        provision_factor: float = 0.8,
        max_models: int = 0,
    ):
        """
        Args:
            cluster_name: Name prefix for CVM discovery (e.g. "bird-tracker").
                         CVMs are expected to be named "{cluster_name}-registry",
                         "{cluster_name}-runner-001", etc.
            spawntee_port: Port where spawntee API listens on each CVM.
            request_timeout: HTTP timeout for spawntee API calls.
            phala_api_url: Phala Cloud API base URL.
            runner_compose_path: Path to docker-compose.phala.runner.yml for provisioning.
            fallback_urls: Optional URL templates for local dev (used when cluster_name
                          is empty or Phala API is unavailable).
            instance_type: Phala CVM instance type for new runners (e.g. "tdx.medium").
            memory_per_model_mb: Estimated memory per model container in MB.
            provision_factor: Fraction of max_models_per_cvm at which to provision
                            a new CVM (0.0–1.0).
            max_models: Global cap on total models across the cluster. 0 = unlimited.
        """
        self.cluster_name = cluster_name
        self.spawntee_port = spawntee_port
        self.request_timeout = request_timeout
        self.phala_api_url = phala_api_url or DEFAULT_PHALA_API_URL
        self.phala_api_key = os.environ.get("PHALA_API_KEY", "")
        self.runner_compose_path = runner_compose_path
        self._fallback_urls = fallback_urls or []

        # Capacity planning
        self.instance_type = instance_type
        self.memory_per_model_mb = memory_per_model_mb
        self.provision_factor = max(0.0, min(1.0, provision_factor))
        self.max_models = max_models  # 0 = unlimited

        instance_memory = INSTANCE_TYPE_MEMORY_MB.get(instance_type)
        if instance_memory is None:
            raise PhalaClusterError(
                f"Unknown instance type '{instance_type}'. "
                f"Known types: {', '.join(sorted(INSTANCE_TYPE_MEMORY_MB))}"
            )

        available_mb = instance_memory - SYSTEM_OVERHEAD_MB
        self.max_models_per_cvm = max(1, math.floor(available_mb / memory_per_model_mb))
        self.provision_threshold = max(1, math.floor(self.max_models_per_cvm * self.provision_factor))

        logger.info(
            "📐 Capacity planning: instance=%s (%d MB), overhead=%d MB, "
            "per_model=%d MB → max_models_per_cvm=%d, provision_at=%d, "
            "global_max=%s",
            instance_type, instance_memory, SYSTEM_OVERHEAD_MB,
            memory_per_model_mb, self.max_models_per_cvm, self.provision_threshold,
            max_models or "unlimited",
        )

        # Discovered CVMs: app_id → CVMInfo
        self.cvms: dict[str, CVMInfo] = {}

        # Head CVM: the one currently accepting new models
        self.head_id: str | None = None

        # Task routing: task_id → app_id
        self.task_client_map: dict[str, str] = {}

    # ─── Discovery ───

    def discover(self):
        """
        Discover CVMs from the Phala Cloud API and probe each one.

        Falls back to configured URLs if cluster_name is empty or
        the Phala API is unavailable (local dev).
        """
        if self.cluster_name and self.phala_api_key:
            self._discover_from_api()
        elif self._fallback_urls:
            self._discover_from_fallback()
        else:
            logger.warning("⚠️ No cluster_name/API key and no fallback URLs — no CVMs discovered")
            return

        if not self.cvms:
            logger.warning("⚠️ No CVMs discovered")
            return

        # Determine head: the last CVM that still has capacity
        # Check in reverse order (newest runners first, then registry)
        for cvm in reversed(list(self.cvms.values())):
            try:
                if cvm.client.has_capacity():
                    self.head_id = cvm.app_id
                    logger.info("📍 Head CVM: %s (%s)", cvm.name, cvm.app_id)
                    break
            except Exception:
                continue

        if not self.head_id:
            # Fallback: use the registry as head (even if full — provisioning
            # will handle it when a build is requested)
            registry = next((c for c in self.cvms.values() if "registry" in c.mode), None)
            if registry:
                self.head_id = registry.app_id
                logger.warning("⚠️ No CVM has capacity, defaulting head to registry: %s", registry.name)
            else:
                # Last resort: first CVM
                self.head_id = next(iter(self.cvms))
                logger.warning("⚠️ No registry found, defaulting head to: %s", self.head_id)

        logger.info("✅ Discovered %d CVM(s), head=%s", len(self.cvms), self.head_id)

    def _discover_from_api(self):
        """Query Phala Cloud API for CVMs matching our cluster name prefix."""
        logger.info("🔍 Discovering CVMs from Phala API (prefix=%s)...", self.cluster_name)

        try:
            headers = {"X-API-Key": self.phala_api_key}
            response = requests.get(
                f"{self.phala_api_url}/api/v1/cvms",
                headers=headers,
                timeout=15,
            )
            response.raise_for_status()
            all_cvms = response.json()
        except Exception as e:
            logger.error("❌ Phala API request failed: %s", e)
            if self._fallback_urls:
                logger.info("  Falling back to configured URLs")
                self._discover_from_fallback()
            return

        # Filter by name prefix
        matching = [c for c in all_cvms if c.get("name", "").startswith(self.cluster_name)]
        logger.info("  Found %d CVM(s) matching prefix '%s' (out of %d total)",
                     len(matching), self.cluster_name, len(all_cvms))

        for cvm_data in matching:
            app_id = cvm_data["app_id"]
            name = cvm_data.get("name", "")
            node_name = cvm_data.get("node_info", {}).get("name", "")
            status = cvm_data.get("status", "")

            if status != "running":
                logger.info("  Skipping %s (%s) — status=%s", name, app_id, status)
                continue

            # Build URL template from app_id and node_name
            url_template = f"https://{app_id}-<model-port>.dstack-pha-{node_name}.phala.network"
            client = SpawnteeClient(
                cluster_url_template=url_template,
                spawntee_port=self.spawntee_port,
                timeout=self.request_timeout,
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

    def _discover_from_fallback(self):
        """Build clients from configured fallback URLs (local dev / no Phala API)."""
        logger.info("🔍 Discovering CVMs from fallback URLs (%d configured)...", len(self._fallback_urls))

        for i, url_template in enumerate(self._fallback_urls):
            client = SpawnteeClient(
                cluster_url_template=url_template,
                spawntee_port=self.spawntee_port,
                timeout=self.request_timeout,
            )

            mode = self._probe_mode(client, f"fallback-{i}")
            if mode is None:
                continue

            # Extract a pseudo app_id from the URL
            # e.g. "https://abc123-<model-port>.dstack..." → "abc123"
            try:
                host_part = url_template.split("//")[1].split("-<model-port>")[0]
            except (IndexError, ValueError):
                host_part = f"fallback-{i}"

            cvm_info = CVMInfo(
                app_id=host_part,
                name=f"fallback-{i}",
                client=client,
                mode=mode,
            )
            self.cvms[host_part] = cvm_info
            logger.info("  ✅ fallback-%d (%s) mode=%s", i, host_part, mode)

    def _get_node_name(self, app_id: str) -> str:
        """Look up a CVM's node name from the Phala API."""
        try:
            headers = {"X-API-Key": self.phala_api_key}
            response = requests.get(
                f"{self.phala_api_url}/api/v1/cvms/{app_id}",
                headers=headers,
                timeout=15,
            )
            response.raise_for_status()
            cvm = response.json()
            return cvm.get("node_info", {}).get("name", "")
        except Exception as e:
            logger.warning("  Could not look up node_name for %s: %s", app_id, e)
        return ""

    def _probe_mode(self, client: SpawnteeClient, label: str) -> str | None:
        """Probe a CVM's /health endpoint to determine its mode."""
        try:
            health = client.health()
            if health.get("service") != "secure-spawn":
                logger.warning("  ⚠️ %s is not a spawntee service, skipping", label)
                return None
            return health.get("mode", "unknown")
        except SpawnteeClientError as e:
            logger.warning("  ⚠️ %s unreachable: %s", label, e)
            return None

    # ─── Task routing ───

    def rebuild_task_map(self):
        """
        Scan all CVMs for running models and rebuild the task → CVM mapping.

        Called on startup to recover task routing after an orchestrator restart.
        """
        self.task_client_map.clear()
        for app_id, cvm in self.cvms.items():
            try:
                running = cvm.client.get_running_models()
                for model in running:
                    task_id = model.get("task_id")
                    if task_id:
                        self.task_client_map[task_id] = app_id
                logger.info("  📋 %s: %d running model(s)", cvm.name, len(running))
            except SpawnteeClientError as e:
                logger.warning("  ⚠️ Could not scan %s: %s", cvm.name, e)

        logger.info("✅ Task map rebuilt: %d task(s) across %d CVM(s)",
                     len(self.task_client_map), len(self.cvms))

    def register_task(self, task_id: str, app_id: str):
        """Record which CVM owns a task (called after build/start)."""
        self.task_client_map[task_id] = app_id

    # ─── Client access ───

    def head_client(self) -> SpawnteeClient:
        """
        Get the head CVM's client (the one accepting new models).

        Raises PhalaClusterError if no head is set.
        """
        if not self.head_id or self.head_id not in self.cvms:
            raise PhalaClusterError("No head CVM available")
        return self.cvms[self.head_id].client

    def client_for_task(self, task_id: str) -> SpawnteeClient:
        """
        Get the client for the CVM that owns a given task.

        Falls back to head client if the task is not in the map
        (e.g. first poll after build, before map is updated).
        """
        app_id = self.task_client_map.get(task_id)
        if app_id and app_id in self.cvms:
            return self.cvms[app_id].client
        # Fallback: try all CVMs (task might exist on a CVM we haven't mapped yet)
        logger.debug("Task %s not in map, falling back to scan", task_id)
        return self.head_client()

    def all_clients(self) -> list[tuple[str, SpawnteeClient]]:
        """Return all (app_id, client) pairs for scanning operations."""
        return [(app_id, cvm.client) for app_id, cvm in self.cvms.items()]

    # ─── Capacity management ───

    def total_running_models(self) -> int:
        """Count models across all CVMs based on the task routing map."""
        return len(self.task_client_map)

    def head_model_count(self) -> int:
        """Count how many models are assigned to the current head CVM."""
        if not self.head_id:
            return 0
        return sum(1 for app_id in self.task_client_map.values() if app_id == self.head_id)

    def ensure_capacity(self):
        """
        Check if the head CVM has capacity. If not, provision a new runner.

        Decision logic (checked in order):
        1. Global cap: if total_running_models >= max_models, refuse.
        2. Orchestrator-side threshold: if head has >= provision_threshold
           models, provision a new CVM.
        3. CVM-side safety net: if the CVM itself reports
           accepting_new_models=false (CAPACITY_THRESHOLD), provision.

        Called before each build to ensure we have somewhere to put the model.
        """
        if not self.head_id:
            raise PhalaClusterError("No head CVM available")

        # 1. Global cap
        total = self.total_running_models()
        if self.max_models and total >= self.max_models:
            raise PhalaClusterError(
                f"Global model cap reached ({total}/{self.max_models}). "
                f"Cannot accept new models."
            )

        head = self.cvms[self.head_id]
        head_count = self.head_model_count()

        # 2. Orchestrator-side threshold (calculated from instance type + memory per model)
        if head_count >= self.provision_threshold:
            logger.info(
                "📊 Head CVM %s has %d/%d models (threshold=%d), provisioning new runner...",
                head.name, head_count, self.max_models_per_cvm, self.provision_threshold,
            )
            self._provision_new_runner()
            return

        # 3. CVM-side safety net (disk/memory based CAPACITY_THRESHOLD)
        if not head.client.has_capacity():
            logger.info(
                "📊 Head CVM %s reports no capacity (CAPACITY_THRESHOLD safety net), "
                "provisioning new runner...",
                head.name,
            )
            self._provision_new_runner()
            return

        logger.debug(
            "✅ Head CVM %s has capacity: %d/%d models (threshold=%d, global=%d/%s)",
            head.name, head_count, self.max_models_per_cvm, self.provision_threshold,
            total, self.max_models or "∞",
        )

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

        # Determine runner number
        existing_runners = [c for c in self.cvms.values() if c.mode == "runner"]
        runner_num = len(existing_runners) + 1
        cvm_name = f"{self.cluster_name}-runner-{runner_num:03d}"

        logger.info("🆕 Provisioning CVM: %s", cvm_name)

        # Get registry URL for the runner's REGISTRY_URL env var
        registry_cvm = next(
            (c for c in self.cvms.values() if "registry" in c.mode), None
        )
        if not registry_cvm:
            raise PhalaClusterError("No registry CVM found — cannot set REGISTRY_URL for runner")

        registry_url = f"https://{registry_cvm.app_id}-{self.spawntee_port}.dstack-pha-{registry_cvm.node_name}.phala.network"

        # Deploy via phala CLI (handles compose hashing, API format, etc.)
        cmd = [
            "phala", "deploy",
            "--name", cvm_name,
            "--instance-type", self.instance_type,
            "--compose", self.runner_compose_path,
            "-e", f"REGISTRY_URL={registry_url}",
            "-e", f"CAPACITY_THRESHOLD={os.environ.get('CAPACITY_THRESHOLD', '0.8')}",
            "--json",
            "--wait",
        ]

        logger.info("  Running: %s", " ".join(cmd))

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=300,  # 5 minute timeout for deploy + wait
                env={**os.environ, "PHALA_API_KEY": self.phala_api_key},
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
        if not node_name:
            raise PhalaClusterError(
                f"CVM {cvm_name} deployed (app_id={new_app_id}) but could not determine node_name from API"
            )

        logger.info("  ✅ CVM created: %s (app_id=%s)", cvm_name, new_app_id)

        # Build client and wait for healthy
        url_template = f"https://{new_app_id}-<model-port>.dstack-pha-{node_name}.phala.network"
        new_client = SpawnteeClient(
            cluster_url_template=url_template,
            spawntee_port=self.spawntee_port,
            timeout=self.request_timeout,
        )

        max_wait = 180  # 3 minutes
        elapsed = 0
        interval = 10
        while elapsed < max_wait:
            try:
                health = new_client.health()
                if health.get("status") == "healthy":
                    logger.info("  ✅ CVM %s is healthy (took %ds)", cvm_name, elapsed)
                    break
            except SpawnteeClientError:
                pass
            time.sleep(interval)
            elapsed += interval
        else:
            raise PhalaClusterError(
                f"CVM {cvm_name} did not become healthy within {max_wait}s"
            )

        # Add to cluster and make it the new head
        cvm_info = CVMInfo(
            app_id=new_app_id,
            name=cvm_name,
            client=new_client,
            mode="runner",
            node_name=node_name,
        )
        self.cvms[new_app_id] = cvm_info
        self.head_id = new_app_id
        logger.info("  📍 New head CVM: %s (%s)", cvm_name, new_app_id)
