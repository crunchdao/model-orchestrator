"""
Tests for PhalaCluster — CVM discovery, head tracking, task routing, capacity.
"""

import tempfile
from unittest.mock import MagicMock, patch

import pytest

from model_orchestrator.infrastructure.phala._cluster import (
    CVMInfo,
    PhalaCluster,
    PhalaClusterError,
)
from model_orchestrator.infrastructure.phala._client import SpawnteeClient, SpawnteeClientError  # noqa: F401


@pytest.fixture(autouse=True)
def _patch_gateway_credentials(tmp_path):
    """Patch GatewayCredentials and provide a dummy key file for all tests."""
    dummy_key = tmp_path / "key.pem"
    dummy_key.write_text("stub")

    class _StubGatewayCredentials:
        @classmethod
        def from_pem(cls, key_pem):
            return cls()

    with patch(
        "model_orchestrator.infrastructure.phala._cluster.GatewayCredentials",
        _StubGatewayCredentials,
    ):
        _orig_init = PhalaCluster.__init__

        def _patched_init(self, *args, **kwargs):
            kwargs.setdefault("gateway_key_path", str(dummy_key))
            _orig_init(self, *args, **kwargs)

        with patch.object(PhalaCluster, "__init__", _patched_init):
            yield


@pytest.fixture
def mock_client_factory():
    """Factory that creates mock SpawnteeClients with configurable responses."""

    def _make(mode="registry+runner", has_capacity=True, running_models=None,
              total_memory_mb=4096):
        client = MagicMock(spec=SpawnteeClient)
        client.health.return_value = {
            "status": "healthy",
            "service": "secure-spawn",
            "mode": mode,
        }
        client.has_capacity.return_value = has_capacity
        client.get_running_models.return_value = running_models or []
        client.capacity.return_value = {
            "total_memory_mb": total_memory_mb,
            "available_memory_mb": total_memory_mb // 2,
            "accepting_new_models": has_capacity,
            "running_models": len(running_models or []),
        }
        return client

    return _make


class TestDiscoveryFromAPI:
    """Test CVM discovery via Phala Cloud API."""

    def test_filters_by_name_prefix(self):
        cluster = PhalaCluster(
            cluster_name="bird-tracker",
            phala_api_url="https://mock-api",
        )
        cluster.phala_api_key = "test-key"

        api_response = [
            {"app_id": "aaa111", "name": "bird-tracker-registry", "status": "running",
             "node_info": {"name": "prod10"}},
            {"app_id": "bbb222", "name": "bird-tracker-runner-001", "status": "running",
             "node_info": {"name": "prod10"}},
            {"app_id": "ccc333", "name": "other-crunch-registry", "status": "running",
             "node_info": {"name": "prod10"}},
        ]

        mock_client = MagicMock(spec=SpawnteeClient)
        mock_client.health.return_value = {
            "status": "healthy", "service": "secure-spawn", "mode": "runner",
        }
        mock_client.has_capacity.return_value = True

        with patch("requests.get") as mock_get, \
             patch.object(SpawnteeClient, "__new__", return_value=mock_client):
            mock_get.return_value = MagicMock(status_code=200, json=lambda: api_response)
            mock_get.return_value.raise_for_status = MagicMock()
            cluster.discover()

        # Should find 2 (bird-tracker-*), not the other-crunch one
        assert len(cluster.cvms) == 2
        assert "aaa111" in cluster.cvms
        assert "bbb222" in cluster.cvms
        assert "ccc333" not in cluster.cvms

    def test_skips_non_running_cvms(self):
        cluster = PhalaCluster(
            cluster_name="test",
            phala_api_url="https://mock-api",
        )
        cluster.phala_api_key = "test-key"

        api_response = [
            {"app_id": "aaa", "name": "test-registry", "status": "running",
             "node_info": {"name": "prod10"}},
            {"app_id": "bbb", "name": "test-runner-001", "status": "stopped",
             "node_info": {"name": "prod10"}},
        ]

        mock_client = MagicMock(spec=SpawnteeClient)
        mock_client.health.return_value = {
            "status": "healthy", "service": "secure-spawn", "mode": "registry+runner",
        }
        mock_client.has_capacity.return_value = True

        with patch("requests.get") as mock_get, \
             patch.object(SpawnteeClient, "__new__", return_value=mock_client):
            mock_get.return_value = MagicMock(status_code=200, json=lambda: api_response)
            mock_get.return_value.raise_for_status = MagicMock()
            cluster.discover()

        assert len(cluster.cvms) == 1
        assert "aaa" in cluster.cvms


class TestGetNodeName:
    """Test _get_node_name fetches a single CVM by ID."""

    def test_fetches_single_cvm_by_id(self):
        cluster = PhalaCluster(
            cluster_name="test",
            phala_api_url="https://mock-api",
        )
        cluster.phala_api_key = "test-key"

        cvm_response = {
            "app_id": "abc123",
            "name": "test-runner-001",
            "status": "running",
            "node_info": {"name": "prod10"},
        }

        with patch("requests.get") as mock_get:
            mock_get.return_value = MagicMock(status_code=200, json=lambda: cvm_response)
            mock_get.return_value.raise_for_status = MagicMock()
            result = cluster._get_node_name("abc123")

        assert result == "prod10"
        # Verify it called the single-CVM endpoint, not the list endpoint
        mock_get.assert_called_once_with(
            "https://mock-api/api/v1/cvms/abc123",
            headers={"X-API-Key": "test-key"},
            timeout=15,
        )

    def test_raises_on_missing_node_info(self):
        cluster = PhalaCluster(
            cluster_name="test",
            phala_api_url="https://mock-api",
        )
        cluster.phala_api_key = "test-key"

        cvm_response = {"app_id": "abc123", "name": "test-runner-001", "status": "running"}

        with patch("requests.get") as mock_get:
            mock_get.return_value = MagicMock(status_code=200, json=lambda: cvm_response)
            mock_get.return_value.raise_for_status = MagicMock()
            with pytest.raises(PhalaClusterError, match="no node_info.name"):
                cluster._get_node_name("abc123")

    def test_raises_on_api_error(self):
        cluster = PhalaCluster(
            cluster_name="test",
            phala_api_url="https://mock-api",
        )
        cluster.phala_api_key = "test-key"

        with patch("requests.get") as mock_get:
            mock_get.side_effect = Exception("Connection refused")
            with pytest.raises(Exception, match="Connection refused"):
                cluster._get_node_name("abc123")


class TestHeadTracking:
    """Test head CVM selection based on capacity."""

    def test_head_is_cvm_with_capacity(self):
        cluster = PhalaCluster(cluster_name="")

        # CVM-1: full, CVM-2: has capacity
        client1 = MagicMock(spec=SpawnteeClient)
        client1.has_capacity.return_value = False
        client2 = MagicMock(spec=SpawnteeClient)
        client2.has_capacity.return_value = True

        cluster.cvms = {
            "cvm1": CVMInfo("cvm1", "test-registry", client1, mode="registry+runner"),
            "cvm2": CVMInfo("cvm2", "test-runner-001", client2, mode="runner"),
        }

        # Simulate discover's head selection logic
        for cvm in reversed(list(cluster.cvms.values())):
            if cvm.client.has_capacity():
                cluster.head_id = cvm.app_id
                break

        assert cluster.head_id == "cvm2"

    def test_head_client_raises_when_no_head(self):
        cluster = PhalaCluster(cluster_name="")
        with pytest.raises(PhalaClusterError, match="No head CVM"):
            cluster.head_client()


class TestTaskRouting:
    """Test task_id → CVM mapping."""

    def test_register_and_retrieve(self):
        cluster = PhalaCluster(cluster_name="")

        client1 = MagicMock(spec=SpawnteeClient)
        client2 = MagicMock(spec=SpawnteeClient)
        cluster.cvms = {
            "cvm1": CVMInfo("cvm1", "reg", client1, mode="registry+runner"),
            "cvm2": CVMInfo("cvm2", "run", client2, mode="runner"),
        }
        cluster.head_id = "cvm2"

        cluster.register_task("task-abc", "cvm1")
        cluster.register_task("task-def", "cvm2")

        assert cluster.client_for_task("task-abc") is client1
        assert cluster.client_for_task("task-def") is client2

    def test_unknown_task_raises(self):
        cluster = PhalaCluster(cluster_name="")
        client1 = MagicMock(spec=SpawnteeClient)
        cluster.cvms = {"cvm1": CVMInfo("cvm1", "reg", client1)}
        cluster.head_id = "cvm1"

        with pytest.raises(PhalaClusterError, match="not found in task routing map"):
            cluster.client_for_task("unknown-task")

    def test_rebuild_task_map(self):
        """rebuild_task_map uses /tasks (persisted state) not /running_models.

        Only routable statuses (pending, running, completed) are included.
        Terminal statuses (failed) are excluded since routing is no longer needed.
        """
        cluster = PhalaCluster(cluster_name="")

        client1 = MagicMock(spec=SpawnteeClient)
        client1.get_tasks.return_value = [
            {"task_id": "task-1", "submission_id": "sub-1", "status": "completed"},
            {"task_id": "task-2", "submission_id": "sub-2", "status": "failed"},
        ]
        client2 = MagicMock(spec=SpawnteeClient)
        client2.get_tasks.return_value = [
            {"task_id": "task-3", "submission_id": "sub-3", "status": "completed"},
        ]

        cluster.cvms = {
            "cvm1": CVMInfo("cvm1", "reg", client1),
            "cvm2": CVMInfo("cvm2", "run", client2),
        }
        cluster.head_id = "cvm2"

        cluster.rebuild_task_map()

        assert cluster.task_client_map["task-1"] == "cvm1"
        assert "task-2" not in cluster.task_client_map  # failed tasks are not routable
        assert cluster.task_client_map["task-3"] == "cvm2"
        assert len(cluster.task_client_map) == 2


class TestInitValidation:
    """Test __init__ validation."""

    def test_unknown_instance_type_raises(self):
        with pytest.raises(PhalaClusterError, match="Unknown instance type"):
            PhalaCluster(cluster_name="", instance_type="tdx.nonexistent")

    def test_capacity_params_stored(self):
        """memory_per_model_mb and capacity_threshold are stored for provisioning."""
        cluster = PhalaCluster(
            cluster_name="",
            instance_type="tdx.large",
            memory_per_model_mb=1024,
            capacity_threshold=0.7,
        )
        assert cluster.instance_type == "tdx.large"
        assert cluster.memory_per_model_mb == 1024
        assert cluster.capacity_threshold == 0.7


class TestEnsureCapacity:
    """Test capacity checking and provisioning trigger."""

    def test_head_accepting_no_action(self):
        """When head CVM reports accepting_new_models=true, no provisioning."""
        cluster = PhalaCluster(cluster_name="")
        client = MagicMock(spec=SpawnteeClient)
        client.has_capacity.return_value = True
        cluster.cvms = {"cvm1": CVMInfo("cvm1", "reg", client)}
        cluster.head_id = "cvm1"

        cluster.ensure_capacity()
        client.has_capacity.assert_called_once()

    def test_head_not_accepting_triggers_provision(self):
        """When head reports not accepting and no other CVM has room, provision."""
        cluster = PhalaCluster(cluster_name="test")
        cluster.phala_api_key = ""  # Will fail at provisioning, that's fine

        client = MagicMock(spec=SpawnteeClient)
        client.has_capacity.return_value = False
        cluster.cvms = {"cvm1": CVMInfo("cvm1", "reg", client)}
        cluster.head_id = "cvm1"

        with pytest.raises(PhalaClusterError, match="PHALA_API_KEY"):
            cluster.ensure_capacity()

    def test_switches_head_to_existing_cvm_with_capacity(self):
        """When head is full but another CVM has capacity, switch head."""
        cluster = PhalaCluster(cluster_name="test")

        client_head = MagicMock(spec=SpawnteeClient)
        client_head.has_capacity.return_value = False

        client_runner = MagicMock(spec=SpawnteeClient)
        client_runner.has_capacity.return_value = True

        cluster.cvms = {
            "reg1": CVMInfo("reg1", "test-registry", client_head, mode="registry+runner"),
            "runner1": CVMInfo("runner1", "test-runner-001", client_runner, mode="runner"),
        }
        cluster.head_id = "reg1"

        cluster.ensure_capacity()

        assert cluster.head_id == "runner1"

    def test_provisions_when_no_existing_cvm_has_capacity(self):
        """When all CVMs are full, provision a new runner."""
        cluster = PhalaCluster(cluster_name="test")
        cluster.phala_api_key = ""

        client_reg = MagicMock(spec=SpawnteeClient)
        client_reg.has_capacity.return_value = False

        client_runner = MagicMock(spec=SpawnteeClient)
        client_runner.has_capacity.return_value = False

        cluster.cvms = {
            "reg1": CVMInfo("reg1", "test-registry", client_reg, mode="registry+runner"),
            "runner1": CVMInfo("runner1", "test-runner-001", client_runner, mode="runner"),
        }
        cluster.head_id = "reg1"

        with pytest.raises(PhalaClusterError, match="PHALA_API_KEY"):
            cluster.ensure_capacity()

        # Head should NOT have changed (provisioning failed)
        assert cluster.head_id == "reg1"

    def test_global_cap_raises_error(self):
        """When total models >= max_models, refuse new models."""
        cluster = PhalaCluster(cluster_name="test", max_models=3)

        client = MagicMock(spec=SpawnteeClient)
        client.has_capacity.return_value = True
        cluster.cvms = {"cvm1": CVMInfo("cvm1", "reg", client)}
        cluster.head_id = "cvm1"

        # 3 tasks across cluster → global cap hit
        cluster.task_client_map = {"t1": "cvm1", "t2": "cvm1", "t3": "cvm1"}

        with pytest.raises(PhalaClusterError, match="Global model cap reached"):
            cluster.ensure_capacity()

    def test_global_cap_zero_means_unlimited(self):
        """max_models=0 means no global limit."""
        cluster = PhalaCluster(cluster_name="", max_models=0)

        client = MagicMock(spec=SpawnteeClient)
        client.has_capacity.return_value = True
        cluster.cvms = {"cvm1": CVMInfo("cvm1", "reg", client)}
        cluster.head_id = "cvm1"

        # 100 tasks, no global cap
        cluster.task_client_map = {f"t{i}": "cvm1" for i in range(100)}

        cluster.ensure_capacity()

    def test_model_counts(self):
        """Test total_running_models and head_model_count."""
        cluster = PhalaCluster(cluster_name="")
        client1 = MagicMock(spec=SpawnteeClient)
        client2 = MagicMock(spec=SpawnteeClient)
        cluster.cvms = {
            "cvm1": CVMInfo("cvm1", "reg", client1),
            "cvm2": CVMInfo("cvm2", "run", client2),
        }
        cluster.head_id = "cvm2"
        cluster.task_client_map = {
            "t1": "cvm1", "t2": "cvm1",
            "t3": "cvm2",
        }

        assert cluster.total_running_models() == 3
        assert cluster.head_model_count() == 1

    def test_approve_runner_hashes_pushes_to_registry(self):
        """_approve_runner_hashes_on_registry reads runner hash from API and calls registry."""
        cluster = PhalaCluster(
            cluster_name="test",
            phala_api_url="https://mock-api",
        )
        cluster.phala_api_key = "test-key"

        client_reg = MagicMock(spec=SpawnteeClient)
        client_reg.approve_hashes.return_value = {"approved_count": 1, "hashes": ["abc123"]}

        client_runner = MagicMock(spec=SpawnteeClient)

        cluster.cvms = {
            "reg1": CVMInfo("reg1", "test-registry", client_reg, mode="registry+runner"),
            "run1": CVMInfo("run1", "test-runner-001", client_runner, mode="runner"),
        }

        with patch("requests.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.raise_for_status = MagicMock()
            mock_resp.json.return_value = {"compose_hash": "abc123"}
            mock_get.return_value = mock_resp

            cluster._approve_runner_hashes_on_registry()

        client_reg.approve_hashes.assert_called_once_with(["abc123"])

    def test_approve_hashes_skipped_when_no_runners(self):
        """No approve call when there are no runner CVMs."""
        cluster = PhalaCluster(cluster_name="test", phala_api_url="https://mock-api")
        cluster.phala_api_key = "test-key"

        client_reg = MagicMock(spec=SpawnteeClient)
        cluster.cvms = {
            "reg1": CVMInfo("reg1", "test-registry", client_reg, mode="registry+runner"),
        }

        cluster._approve_runner_hashes_on_registry()
        client_reg.approve_hashes.assert_not_called()

    def test_all_clients_returns_all(self):
        cluster = PhalaCluster(cluster_name="")
        client1 = MagicMock(spec=SpawnteeClient)
        client2 = MagicMock(spec=SpawnteeClient)
        cluster.cvms = {
            "cvm1": CVMInfo("cvm1", "reg", client1),
            "cvm2": CVMInfo("cvm2", "run", client2),
        }

        result = cluster.all_clients()
        assert len(result) == 2
        assert ("cvm1", client1) in result
        assert ("cvm2", client2) in result


class TestUpdateVPCAllowedApps:
    """Test _update_vpc_allowed_apps() — builds VPC_ALLOWED_APPS and shells out correctly."""

    def _make_cluster(self, vpc_server_cvm_id="vpc-server-app-id", vpc_server_compose_path="/path/to/vpc-server.yml"):
        cluster = PhalaCluster(
            cluster_name="bird-tracker",
            phala_api_url="https://mock-api",
            vpc_enabled=True,
            vpc_server_cvm_id=vpc_server_cvm_id,
            vpc_server_compose_path=vpc_server_compose_path,
        )
        cluster.phala_api_key = "test-key"
        return cluster

    def test_builds_allowed_apps_from_registry_and_runners(self):
        """VPC_ALLOWED_APPS includes registry app_id and all runner app_ids."""
        cluster = self._make_cluster()

        client_reg = MagicMock(spec=SpawnteeClient)
        client_r1 = MagicMock(spec=SpawnteeClient)
        client_r2 = MagicMock(spec=SpawnteeClient)

        cluster.cvms = {
            "reg-app-id": CVMInfo("reg-app-id", "bird-tracker-registry", client_reg, mode="registry+runner"),
            "runner-1-id": CVMInfo("runner-1-id", "bird-tracker-runner-aaa", client_r1, mode="runner"),
            "runner-2-id": CVMInfo("runner-2-id", "bird-tracker-runner-bbb", client_r2, mode="runner"),
        }

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            cluster._update_vpc_allowed_apps()

        mock_run.assert_called_once()
        call_args = mock_run.call_args[0][0]  # positional cmd list

        assert call_args[0] == "phala"
        assert call_args[1] == "cvms"
        assert call_args[2] == "upgrade"
        assert call_args[3] == "vpc-server-app-id"

        # --compose must always be present (bare -e without --compose is unverified)
        assert "--compose" in call_args
        compose_idx = call_args.index("--compose")
        assert call_args[compose_idx + 1] == "/path/to/vpc-server.yml"

        # Find the VPC_ALLOWED_APPS value in the cmd
        vpc_apps_value = None
        for i, arg in enumerate(call_args):
            if arg == "-e" and i + 1 < len(call_args):
                if call_args[i + 1].startswith("VPC_ALLOWED_APPS="):
                    vpc_apps_value = call_args[i + 1].split("=", 1)[1]

        assert vpc_apps_value is not None, "VPC_ALLOWED_APPS not found in command"
        allowed = set(vpc_apps_value.split(","))
        assert allowed == {"reg-app-id", "runner-1-id", "runner-2-id"}

    def test_raises_when_vpc_server_compose_path_not_set(self):
        """Raises PhalaClusterError when vpc_server_compose_path is empty."""
        cluster = self._make_cluster(vpc_server_compose_path="")

        client_reg = MagicMock(spec=SpawnteeClient)
        cluster.cvms = {
            "reg-app-id": CVMInfo("reg-app-id", "bird-tracker-registry", client_reg, mode="registry+runner"),
        }

        with pytest.raises(PhalaClusterError, match="vpc_server_compose_path is not configured"):
            cluster._update_vpc_allowed_apps()

    def test_raises_when_vpc_server_cvm_id_not_set(self):
        """Raises PhalaClusterError when vpc_server_cvm_id is empty."""
        cluster = self._make_cluster(vpc_server_cvm_id="")

        client_reg = MagicMock(spec=SpawnteeClient)
        cluster.cvms = {
            "reg-app-id": CVMInfo("reg-app-id", "bird-tracker-registry", client_reg, mode="registry+runner"),
        }

        with pytest.raises(PhalaClusterError, match="vpc_server_cvm_id is not configured"):
            cluster._update_vpc_allowed_apps()

    def test_raises_when_no_registry(self):
        """Raises PhalaClusterError when no registry CVM is in the cluster."""
        cluster = self._make_cluster()
        cluster.cvms = {}  # empty — no registry

        with pytest.raises(PhalaClusterError, match="no registry CVM found"):
            cluster._update_vpc_allowed_apps()

    def test_raises_on_cli_failure(self):
        """Raises PhalaClusterError when phala cvms upgrade returns non-zero."""
        cluster = self._make_cluster()

        client_reg = MagicMock(spec=SpawnteeClient)
        cluster.cvms = {
            "reg-app-id": CVMInfo("reg-app-id", "bird-tracker-registry", client_reg, mode="registry+runner"),
        }

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="some error")
            with pytest.raises(PhalaClusterError, match="phala cvms upgrade failed"):
                cluster._update_vpc_allowed_apps()


class TestVPCProvisioning:
    """Test that _provision_new_runner() behaves correctly with vpc_enabled."""

    def _make_cluster(self, vpc_enabled: bool):
        cluster = PhalaCluster(
            cluster_name="bird-tracker",
            phala_api_url="https://mock-api",
            runner_compose_path="/path/to/runner.yml",
            vpc_enabled=vpc_enabled,
            vpc_server_cvm_id="vpc-server-app-id",
        )
        cluster.phala_api_key = "test-key"

        client_reg = MagicMock(spec=SpawnteeClient)
        cluster.cvms = {
            "reg-app-id": CVMInfo(
                "reg-app-id", "bird-tracker-registry", client_reg,
                mode="registry+runner", node_name="prod10", node_id=17,
            ),
        }
        cluster.head_id = "reg-app-id"
        return cluster

    def test_vpc_enabled_deploy_includes_node_id_and_vpc_env_vars(self):
        """With vpc_enabled=True, deploy cmd includes --node-id, VPC_NODE_NAME, VPC_SERVER_APP_ID."""
        cluster = self._make_cluster(vpc_enabled=True)

        captured_cmds = []

        def fake_run(cmd, **kwargs):
            captured_cmds.append(cmd)
            app_id = "new-runner-id"
            return MagicMock(returncode=0, stdout=f'{{"app_id": "{app_id}"}}', stderr="")

        new_client = MagicMock(spec=SpawnteeClient)
        new_client.health.return_value = {"status": "healthy", "service": "secure-spawn", "mode": "runner"}
        new_client.capacity.return_value = {"accepting_new_models": True}
        new_client.has_capacity.return_value = True

        with patch("subprocess.run", side_effect=fake_run), \
             patch.object(cluster, "_get_node_name", return_value="prod10"), \
             patch.object(cluster, "_approve_runner_hashes_on_registry"), \
             patch("model_orchestrator.infrastructure.phala._cluster.SpawnteeClient",
                   return_value=new_client):
            cluster._provision_new_runner()

        # First call is always `phala deploy`
        deploy_cmd = captured_cmds[0]
        deploy_str = " ".join(deploy_cmd)

        assert "--node-id" in deploy_cmd
        assert "17" in deploy_cmd  # registry's node_id
        assert "VPC_NODE_NAME=" in deploy_str
        assert "VPC_SERVER_APP_ID=vpc-server-app-id" in deploy_str
        # REGISTRY_URL must be VPC-internal
        assert "http://bird-tracker-registry.dstack.internal:" in deploy_str

    def test_vpc_disabled_deploy_has_no_node_id_and_public_registry_url(self):
        """With vpc_enabled=False, no --node-id, no VPC env vars, public REGISTRY_URL."""
        cluster = self._make_cluster(vpc_enabled=False)

        captured_cmds = []

        def fake_run(cmd, **kwargs):
            captured_cmds.append(cmd)
            app_id = "new-runner-id"
            return MagicMock(returncode=0, stdout=f'{{"app_id": "{app_id}"}}', stderr="")

        new_client = MagicMock(spec=SpawnteeClient)
        new_client.health.return_value = {"status": "healthy", "service": "secure-spawn", "mode": "runner"}
        new_client.capacity.return_value = {"accepting_new_models": True}
        new_client.has_capacity.return_value = True

        with patch("subprocess.run", side_effect=fake_run), \
             patch.object(cluster, "_get_node_name", return_value="prod10"), \
             patch.object(cluster, "_approve_runner_hashes_on_registry"), \
             patch("model_orchestrator.infrastructure.phala._cluster.SpawnteeClient",
                   return_value=new_client):
            cluster._provision_new_runner()

        deploy_cmd = captured_cmds[0]
        deploy_str = " ".join(deploy_cmd)

        assert "--node-id" not in deploy_cmd
        assert "VPC_NODE_NAME" not in deploy_str
        assert "VPC_SERVER_APP_ID" not in deploy_str
        # REGISTRY_URL must be the public gateway URL
        assert "https://reg-app-id-" in deploy_str
        assert ".phala.network" in deploy_str

    def test_vpc_enabled_calls_update_vpc_allowed_apps(self):
        """With vpc_enabled=True, _update_vpc_allowed_apps() is called during provision."""
        cluster = self._make_cluster(vpc_enabled=True)

        def fake_run(cmd, **kwargs):
            return MagicMock(returncode=0, stdout='{"app_id": "new-runner-id"}', stderr="")

        new_client = MagicMock(spec=SpawnteeClient)
        new_client.health.return_value = {"status": "healthy", "service": "secure-spawn", "mode": "runner"}
        new_client.capacity.return_value = {"accepting_new_models": True}
        new_client.has_capacity.return_value = True

        with patch("subprocess.run", side_effect=fake_run), \
             patch.object(cluster, "_get_node_name", return_value="prod10"), \
             patch.object(cluster, "_approve_runner_hashes_on_registry"), \
             patch.object(cluster, "_update_vpc_allowed_apps") as mock_update_vpc, \
             patch("model_orchestrator.infrastructure.phala._cluster.SpawnteeClient",
                   return_value=new_client):
            cluster._provision_new_runner()

        mock_update_vpc.assert_called_once()

    def test_vpc_disabled_does_not_call_update_vpc_allowed_apps(self):
        """With vpc_enabled=False, _update_vpc_allowed_apps() is never called."""
        cluster = self._make_cluster(vpc_enabled=False)

        def fake_run(cmd, **kwargs):
            return MagicMock(returncode=0, stdout='{"app_id": "new-runner-id"}', stderr="")

        new_client = MagicMock(spec=SpawnteeClient)
        new_client.health.return_value = {"status": "healthy", "service": "secure-spawn", "mode": "runner"}
        new_client.capacity.return_value = {"accepting_new_models": True}
        new_client.has_capacity.return_value = True

        with patch("subprocess.run", side_effect=fake_run), \
             patch.object(cluster, "_get_node_name", return_value="prod10"), \
             patch.object(cluster, "_approve_runner_hashes_on_registry"), \
             patch.object(cluster, "_update_vpc_allowed_apps") as mock_update_vpc, \
             patch("model_orchestrator.infrastructure.phala._cluster.SpawnteeClient",
                   return_value=new_client):
            cluster._provision_new_runner()

        mock_update_vpc.assert_not_called()

    def test_vpc_server_skipped_during_discover(self):
        """When vpc_enabled, the VPC server CVM is not probed as a spawntee service."""
        cluster = PhalaCluster(
            cluster_name="bird-tracker",
            phala_api_url="https://mock-api",
            vpc_enabled=True,
            vpc_server_cvm_id="vpc-server-app-id",
        )
        cluster.phala_api_key = "test-key"

        api_response = [
            {"app_id": "reg-app-id", "name": "bird-tracker-registry", "status": "running",
             "node_info": {"name": "prod10", "id": 17}},
            {"app_id": "vpc-server-app-id", "name": "bird-tracker-vpc-server", "status": "running",
             "node_info": {"name": "prod10", "id": 17}},
        ]

        mock_client = MagicMock(spec=SpawnteeClient)
        mock_client.health.return_value = {
            "status": "healthy", "service": "secure-spawn", "mode": "registry+runner",
        }
        mock_client.has_capacity.return_value = True

        with patch("requests.get") as mock_get, \
             patch.object(SpawnteeClient, "__new__", return_value=mock_client):
            mock_get.return_value = MagicMock(status_code=200, json=lambda: api_response)
            mock_get.return_value.raise_for_status = MagicMock()
            cluster._discover_from_api()

        # VPC server should not appear in cvms — only the registry
        assert "vpc-server-app-id" not in cluster.cvms
        assert "reg-app-id" in cluster.cvms
        # Health was only probed once (registry), not for the VPC server
        assert mock_client.health.call_count == 1

    def test_node_id_stored_on_cvm_info(self):
        """node_id from node_info.id is stored on CVMInfo during discovery."""
        cluster = PhalaCluster(
            cluster_name="bird-tracker",
            phala_api_url="https://mock-api",
            vpc_enabled=False,
        )
        cluster.phala_api_key = "test-key"

        api_response = [
            {"app_id": "reg-app-id", "name": "bird-tracker-registry", "status": "running",
             "node_info": {"name": "prod10", "id": 17}},
        ]

        mock_client = MagicMock(spec=SpawnteeClient)
        mock_client.health.return_value = {
            "status": "healthy", "service": "secure-spawn", "mode": "registry+runner",
        }
        mock_client.has_capacity.return_value = True

        with patch("requests.get") as mock_get, \
             patch.object(SpawnteeClient, "__new__", return_value=mock_client):
            mock_get.return_value = MagicMock(status_code=200, json=lambda: api_response)
            mock_get.return_value.raise_for_status = MagicMock()
            cluster._discover_from_api()

        assert cluster.cvms["reg-app-id"].node_id == 17
