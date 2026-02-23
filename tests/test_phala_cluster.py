"""
Tests for PhalaCluster — CVM discovery, head tracking, task routing, capacity.
"""

from unittest.mock import MagicMock, patch

import pytest

from model_orchestrator.infrastructure.phala._cluster import (
    CVMInfo,
    PhalaCluster,
    PhalaClusterError,
)
from model_orchestrator.infrastructure.phala._client import SpawnteeClient, SpawnteeClientError  # noqa: F401


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

    def test_unknown_task_falls_back_to_head(self):
        cluster = PhalaCluster(cluster_name="")
        client1 = MagicMock(spec=SpawnteeClient)
        cluster.cvms = {"cvm1": CVMInfo("cvm1", "reg", client1)}
        cluster.head_id = "cvm1"

        result = cluster.client_for_task("unknown-task")
        assert result is client1

    def test_rebuild_task_map(self):
        cluster = PhalaCluster(cluster_name="")

        client1 = MagicMock(spec=SpawnteeClient)
        client1.get_running_models.return_value = [
            {"task_id": "task-1", "submission_id": "sub-1"},
            {"task_id": "task-2", "submission_id": "sub-2"},
        ]
        client2 = MagicMock(spec=SpawnteeClient)
        client2.get_running_models.return_value = [
            {"task_id": "task-3", "submission_id": "sub-3"},
        ]

        cluster.cvms = {
            "cvm1": CVMInfo("cvm1", "reg", client1),
            "cvm2": CVMInfo("cvm2", "run", client2),
        }
        cluster.head_id = "cvm2"

        cluster.rebuild_task_map()

        assert cluster.task_client_map["task-1"] == "cvm1"
        assert cluster.task_client_map["task-2"] == "cvm1"
        assert cluster.task_client_map["task-3"] == "cvm2"


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
