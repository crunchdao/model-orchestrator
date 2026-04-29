import json
from typing import Any

import requests

from model_orchestrator.entities import Crunch, ModelRun
from model_orchestrator.services import Runner
from model_orchestrator.utils.logging_utils import get_logger

RPC_PORT = 50051


class NomadModelRunner(Runner):

    NOMAD_TO_RUNNER_STATUS = {
        # Task states
        "pending": ModelRun.RunnerStatus.INITIALIZING,
        "running": ModelRun.RunnerStatus.RUNNING,
        "dead": ModelRun.RunnerStatus.STOPPED,
        # Allocation client statuses
        "complete": ModelRun.RunnerStatus.STOPPED,
        "failed": ModelRun.RunnerStatus.FAILED,
        "lost": ModelRun.RunnerStatus.FAILED,
    }

    def __init__(self, config):
        self.nomad_addr = config.nomad_addr
        self.datacenter = config.datacenter
        self.runtime = config.runtime
        self.nomad_token = getattr(config, 'nomad_token', None)
        self.restart_attempts = getattr(config, 'restart_attempts', 2)
        self.restart_interval_s = getattr(config, 'restart_interval_s', 3600)
        self.restart_delay_s = getattr(config, 'restart_delay_s', 30)
        self.reschedule_attempts = getattr(config, 'reschedule_attempts', 1)
        self.reschedule_delay_s = getattr(config, 'reschedule_delay_s', 60)

    def _url(self, path: str) -> str:
        return f"{self.nomad_addr}/v1{path}"

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        if self.nomad_token:
            headers = kwargs.pop("headers", {})
            headers["X-Nomad-Token"] = self.nomad_token
            kwargs["headers"] = headers
        resp = requests.request(method, self._url(path), timeout=30, **kwargs)
        resp.raise_for_status()
        return resp

    def create(self, crunch: Crunch) -> dict:
        return {}

    def run(self, model: ModelRun, crunch: Crunch) -> tuple[str, str, Any]:
        job_id = self._make_job_id(model, crunch)
        hw_config = self._get_hw_config(model, crunch)

        image = model.docker_image
        if crunch.builder_config and "ecr_image_uri" in crunch.builder_config:
            image = f'{crunch.builder_config["ecr_image_uri"]}:{image.split(":")[-1]}'

        env = {
            "SECURE": str(crunch.infrastructure.is_secure),
            "MODEL_ID": model.model_id,
            "CRUNCH_ONCHAIN_ADDRESS": crunch.onchain_address or "",
            "CRUNCHER_WALLET_PUBKEY": model.cruncher_onchain_info.wallet_pubkey or "",
            "CRUNCHER_HOTKEY": model.cruncher_onchain_info.hotkey or "",
            "COORDINATOR_WALLET_PUBKEY": (crunch.coordinator_info.wallet_pubkey if crunch.coordinator_info else "") or "",
            "COORDINATOR_CERT_HASH": (crunch.coordinator_info.cert_hash if crunch.coordinator_info else "") or "",
            "COORDINATOR_CERT_HASH_SECONDARY": (crunch.coordinator_info.cert_hash_secondary if crunch.coordinator_info else "") or "",
        }
        env.update(crunch.resolve_runner_envs())

        # CPU: reservation only, no hard limit (containers can burst on idle CPU)
        cpu_mhz = int(hw_config.vcpus * 2500)
        # Memory: reservation for scheduling, max for OOM kill
        memory_mb = hw_config.memory_reservation or hw_config.memory
        memory_max_mb = hw_config.memory

        job_spec = {
            "Job": {
                "ID": job_id,
                "Name": job_id,
                "Type": "service",
                "Datacenters": [self.datacenter],
                "TaskGroups": [
                    {
                        "Name": "model",
                        "Count": 1,
                        "RestartPolicy": {
                            "Attempts": self.restart_attempts,
                            "Interval": self.restart_interval_s * 1_000_000_000,
                            "Delay": self.restart_delay_s * 1_000_000_000,
                            "Mode": "fail",
                        },
                        "ReschedulePolicy": {
                            "Attempts": self.reschedule_attempts,
                            "Interval": self.restart_interval_s * 1_000_000_000,
                            "Delay": self.reschedule_delay_s * 1_000_000_000,
                            "Unlimited": False,
                        },
                        "Update": {
                            "HealthyDeadline": 540000000000,  # 9min
                        },
                        "EphemeralDisk": {
                            "SizeMB": 8192,
                            "Migrate": False,
                            "Sticky": False,
                        },
                        "Networks": [
                            {
                                "Mode": crunch.infrastructure.network_mode,
                                "DynamicPorts": [
                                    {"Label": "grpc", "To": RPC_PORT}
                                ]
                            }
                        ],
                        "Tasks": [
                            {
                                "Name": "runner",
                                "Driver": "docker",
                                "Config": {
                                    "image": image,
                                    "ports": ["grpc"],
                                    "runtime": self.runtime,
                                    "cpu_hard_limit": True,
                                },
                                "Env": env,
                                "Resources": {
                                    "CPU": cpu_mhz,
                                    "MemoryMB": memory_mb,
                                    "MemoryMaxMB": memory_max_mb,
                                },
                            }
                        ],
                    }
                ],
                "Meta": {
                    "model_id": model.model_id,
                    "crunch_id": crunch.id,
                    "crunch_name": crunch.name,
                    "code_submission_id": model.code_submission_id,
                },
            }
        }

        self._request("POST", "/jobs", json=job_spec)
        get_logger().debug(f"Nomad: submitted job {job_id}")

        runner_info = {
            "job_id": job_id,
            "datacenter": self.datacenter,
        }

        logs_arn = f"loki:logs:log-query:com_hashicorp_nomad_job_id/{job_id}"

        return job_id, logs_arn, runner_info

    def stop(self, model: ModelRun) -> str:
        job_id = model.runner_info["job_id"]
        try:
            self._request("DELETE", f"/job/{job_id}?purge=false")
            get_logger().debug(f"Nomad: stopped job {job_id}")
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 404:
                get_logger().debug(f"Nomad: job {job_id} not found")
            else:
                raise
        return job_id

    def load_statuses(
        self,
        models: list[ModelRun],
    ) -> dict[ModelRun, tuple[ModelRun.RunnerStatus, str, int]]:
        if not models:
            return {}

        # 2 API calls for all models instead of 3N
        jobs_by_id = {j["ID"]: j for j in self._request("GET", "/jobs").json()}

        allocs_by_job = {}
        for alloc in self._request("GET", "/allocations?resources=true").json():
            job_id = alloc.get("JobID", "")
            if job_id not in allocs_by_job or alloc.get("CreateIndex", 0) > allocs_by_job[job_id].get("CreateIndex", 0):
                allocs_by_job[job_id] = alloc

        results = {}
        for model in models:
            job_id = model.runner_info.get("job_id", model.runner_job_id)
            results[model] = self._resolve_status(job_id, jobs_by_id, allocs_by_job)

        return results

    def _resolve_status(
        self, job_id: str, jobs_by_id: dict, allocs_by_job: dict,
    ) -> tuple[ModelRun.RunnerStatus, str | None, int]:
        job = jobs_by_id.get(job_id)
        if not job:
            return ModelRun.RunnerStatus.FAILED, None, 0

        if job.get("Status") == "dead":
            alloc = allocs_by_job.get(job_id)
            if alloc and alloc.get("ClientStatus") == "failed":
                return ModelRun.RunnerStatus.FAILED, None, 0
            return ModelRun.RunnerStatus.STOPPED, None, 0

        alloc = allocs_by_job.get(job_id)
        if not alloc:
            return ModelRun.RunnerStatus.INITIALIZING, None, 0

        task_states = alloc.get("TaskStates", {}) or {}
        task_state = task_states.get("runner", {})
        task_status = task_state.get("State", "")
        client_status = alloc.get("ClientStatus", "")

        if task_state.get("Failed", False) or client_status == "failed":
            return ModelRun.RunnerStatus.RECOVERING, None, 0

        runner_status = self.NOMAD_TO_RUNNER_STATUS.get(
            task_status, ModelRun.RunnerStatus.INITIALIZING
        )

        ip = None
        port = 0
        if runner_status == ModelRun.RunnerStatus.RUNNING:
            alloc_resources = alloc.get("AllocatedResources", {}) or {}
            shared = alloc_resources.get("Shared", {}) or {}
            networks = shared.get("Networks", [])
            if networks:
                ip = networks[0].get("IP")
                for dp in networks[0].get("DynamicPorts", []):
                    if dp.get("Label") == "grpc":
                        port = dp.get("Value", 0)
                        break

        return runner_status, ip, port

    @staticmethod
    def _make_job_id(model: ModelRun, crunch: Crunch) -> str:
        return f"{crunch.name}-{model.model_id}-{model.code_submission_id}"[:64]

    @staticmethod
    def _get_hw_config(model: ModelRun, crunch: Crunch):
        is_gpu = model.hardware_type == ModelRun.HardwareType.GPU
        return crunch.infrastructure.gpu_config if is_gpu else crunch.infrastructure.cpu_config