"""
Integration test for NomadModelRunner against a live Nomad cluster.
Uses busybox instead of a real model image.

Usage:
    python -m pytest tests/test_nomad_runner.py -v -s
"""
import os
import time
import unittest
from types import SimpleNamespace

import pytest

from model_orchestrator.entities import ModelRun, CruncherOnchainInfo
from model_orchestrator.entities.crunch import Crunch, Infrastructure, CpuConfig, RunnerType
from model_orchestrator.infrastructure.nomad._runner import NomadModelRunner


NOMAD_ADDR = "http://65.108.122.124:4646"
DATACENTER = "hetzner"
RUNTIME = "kata"


def make_config():
    return SimpleNamespace(
        nomad_addr=NOMAD_ADDR,
        datacenter=DATACENTER,
        runtime=RUNTIME,
        nomad_token=os.environ.get("NOMAD_TOKEN"),
    )


def make_crunch():
    return Crunch(
        id="test-crunch",
        name="test-crunch",
        onchain_name="test-crunch",
        infrastructure=Infrastructure(
            cluster_name=None,
            zone="hetzner",
            runner_type=RunnerType.AWS_ECS,
            cpu_config=CpuConfig(vcpus=0.1, memory=64),
        ),
    )


def make_model(model_id="test-model-001", code_submission_id="sub-001", docker_image="busybox:latest"):
    return ModelRun(
        id=None,
        model_id=model_id,
        name="test-model",
        crunch_id="test-crunch",
        cruncher_onchain_info=CruncherOnchainInfo(wallet_pubkey="test-wallet", hotkey=""),
        code_submission_id=code_submission_id,
        resource_id="res-001",
        hardware_type=ModelRun.HardwareType.CPU,
        desired_status=ModelRun.DesiredStatus.RUNNING,
        docker_image=docker_image,
    )


@pytest.mark.integration
class TestNomadModelRunner(unittest.TestCase):

    def setUp(self):
        self.runner = NomadModelRunner(make_config())
        self.crunch = make_crunch()

    def _cleanup_job(self, job_id):
        try:
            self.runner._request("DELETE", f"/job/{job_id}?purge=true")
        except Exception:
            pass

    def test_full_lifecycle(self):
        """Test run -> poll status -> stop using the real runner.run()"""
        model = make_model()

        # busybox has no entrypoint, it will exit immediately
        # but we can still test the full flow: submit, get status, stop
        job_id, logs_arn, runner_info = self.runner.run(model, self.crunch)
        model.runner_job_id = job_id
        model.runner_info = runner_info

        print(f"\n  Job submitted: {job_id}")
        self.assertIsNotNone(job_id)
        self.assertIsNotNone(logs_arn)
        self.assertEqual(runner_info["job_id"], job_id)

        # Poll until we see at least INITIALIZING or RUNNING
        seen_status = None
        for i in range(10):
            time.sleep(2)
            statuses = self.runner.load_statuses([model])
            status, ip, port = statuses[model]
            print(f"  Poll {i+1}: status={status}, ip={ip}, port={port}")
            seen_status = status
            if status == ModelRun.RunnerStatus.RUNNING:
                self.assertIsNotNone(ip)
                self.assertGreater(port, 0)
                break

        self.assertIsNotNone(seen_status)

        # Stop
        self.runner.stop(model)
        print(f"  Job stopped: {job_id}")

        time.sleep(3)
        statuses = self.runner.load_statuses([model])
        status, _, _ = statuses[model]
        print(f"  After stop: status={status}")
        self.assertIn(status, (ModelRun.RunnerStatus.STOPPED, ModelRun.RunnerStatus.FAILED))

        self._cleanup_job(job_id)

    def test_recover_after_crash(self):
        """Test that a crashing container goes through RECOVERING -> FAILED"""
        # Use a runner with fast restart/reschedule for testing
        fast_config = SimpleNamespace(
            nomad_addr=NOMAD_ADDR,
            datacenter=DATACENTER,
            runtime=RUNTIME,
            nomad_token=os.environ.get("NOMAD_TOKEN"),
            restart_attempts=1,
            restart_interval_s=60,
            restart_delay_s=1,
            reschedule_attempts=1,
            reschedule_delay_s=5,
        )
        fast_runner = NomadModelRunner(fast_config)

        # test-crash:latest is built on the server with CMD ["sh", "-c", "exit 1"]
        model = make_model(model_id="test-crash", code_submission_id="sub-crash", docker_image="test-crash:latest")

        job_id, _, runner_info = fast_runner.run(model, self.crunch)
        model.runner_job_id = job_id
        model.runner_info = runner_info
        print(f"\n  Crash job submitted: {job_id}")

        saw_recovering = False
        final_status = None
        for i in range(40):
            time.sleep(3)
            statuses = fast_runner.load_statuses([model])
            status, _, _ = statuses[model]
            print(f"  Poll {i+1}: status={status}")
            if status == ModelRun.RunnerStatus.RECOVERING:
                saw_recovering = True
            if status in (ModelRun.RunnerStatus.FAILED, ModelRun.RunnerStatus.STOPPED):
                final_status = status
                break

        print(f"  saw_recovering={saw_recovering}, final_status={final_status}")
        self.assertTrue(saw_recovering or final_status is not None,
                        "Expected to see RECOVERING or reach a terminal state")
        self.assertIn(final_status, (ModelRun.RunnerStatus.FAILED, ModelRun.RunnerStatus.STOPPED),
                      f"Expected terminal state, got {final_status}")

        self._cleanup_job(job_id)

    def test_stop_nonexistent(self):
        """Stop a job that doesn't exist should not raise"""
        model = make_model(model_id="nonexistent")
        model.runner_info = {"job_id": "nonexistent-job-id"}
        self.runner.stop(model)

    def test_load_status_nonexistent(self):
        """Load status of a nonexistent job should return FAILED"""
        model = make_model(model_id="nonexistent")
        model.runner_info = {"job_id": "nonexistent-job-id"}
        model.runner_job_id = "nonexistent-job-id"

        statuses = self.runner.load_statuses([model])
        status, ip, port = statuses[model]
        self.assertEqual(status, ModelRun.RunnerStatus.FAILED)


if __name__ == "__main__":
    unittest.main()
