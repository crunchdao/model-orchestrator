"""
Tests for PhalaCluster — CVM discovery, head tracking, task routing.
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

    def _make(mode="registry+runner", has_capacity=True, running_models=None):
        client = MagicMock(spec=SpawnteeClient)
        client.health.return_value = {
            "status": "healthy",
            "service": "secure-spawn",
            "mode": mode,
        }
        client.has_capacity.return_value = has_capacity
        client.get_running_models.return_value = running_models or []
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

    def test_returns_empty_on_missing_node_info(self):
        cluster = PhalaCluster(
            cluster_name="test",
            phala_api_url="https://mock-api",
        )
        cluster.phala_api_key = "test-key"

        cvm_response = {"app_id": "abc123", "name": "test-runner-001", "status": "running"}

        with patch("requests.get") as mock_get:
            mock_get.return_value = MagicMock(status_code=200, json=lambda: cvm_response)
            mock_get.return_value.raise_for_status = MagicMock()
            result = cluster._get_node_name("abc123")

        assert result == ""

    def test_returns_empty_on_api_error(self):
        cluster = PhalaCluster(
            cluster_name="test",
            phala_api_url="https://mock-api",
        )
        cluster.phala_api_key = "test-key"

        with patch("requests.get") as mock_get:
            mock_get.side_effect = Exception("Connection refused")
            result = cluster._get_node_name("abc123")

        assert result == ""


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


class TestCapacityPlanning:
    """Test capacity calculation from instance type and memory settings."""

    def test_default_medium_1gb_models(self):
        cluster = PhalaCluster(
            cluster_name="",
            instance_type="tdx.medium",  # 4096 MB
            memory_per_model_mb=1024,
        )
        # (4096 - 500) / 1024 = 3.51 → 3
        assert cluster.max_models_per_cvm == 3
        # 3 * 0.8 = 2.4 → 2
        assert cluster.provision_threshold == 2

    def test_large_instance_small_models(self):
        cluster = PhalaCluster(
            cluster_name="",
            instance_type="tdx.large",  # 8192 MB
            memory_per_model_mb=512,
        )
        # (8192 - 500) / 512 = 15.02 → 15
        assert cluster.max_models_per_cvm == 15
        # 15 * 0.8 = 12
        assert cluster.provision_threshold == 12

    def test_small_instance_big_model_minimum_one(self):
        cluster = PhalaCluster(
            cluster_name="",
            instance_type="tdx.small",  # 2048 MB
            memory_per_model_mb=4096,
        )
        # (2048 - 500) / 4096 = 0.37 → clamped to 1
        assert cluster.max_models_per_cvm == 1
        assert cluster.provision_threshold == 1

    def test_unknown_instance_type_raises(self):
        with pytest.raises(PhalaClusterError, match="Unknown instance type"):
            PhalaCluster(cluster_name="", instance_type="tdx.nonexistent")

    def test_provision_factor_clamped(self):
        cluster = PhalaCluster(
            cluster_name="",
            instance_type="tdx.large",
            memory_per_model_mb=1024,
            provision_factor=1.5,  # should be clamped to 1.0
        )
        # (8192 - 500) / 1024 = 7.51 → 7
        assert cluster.max_models_per_cvm == 7
        # 7 * 1.0 = 7
        assert cluster.provision_threshold == 7


class TestEnsureCapacity:
    """Test capacity checking and provisioning trigger."""

    def test_head_has_capacity_no_action(self):
        cluster = PhalaCluster(
            cluster_name="",
            instance_type="tdx.medium",
            memory_per_model_mb=1024,
        )
        client = MagicMock(spec=SpawnteeClient)
        client.has_capacity.return_value = True
        cluster.cvms = {"cvm1": CVMInfo("cvm1", "reg", client)}
        cluster.head_id = "cvm1"

        # No tasks → 0 models on head, threshold=2 → fine
        cluster.ensure_capacity()
        client.has_capacity.assert_called_once()

    def test_orchestrator_threshold_triggers_provision(self):
        """When head has >= provision_threshold models, provision a new CVM."""
        cluster = PhalaCluster(
            cluster_name="test",
            instance_type="tdx.medium",  # max_per_cvm=3, threshold=2
            memory_per_model_mb=1024,
        )
        cluster.phala_api_key = ""  # Will fail at provisioning, that's fine
        client = MagicMock(spec=SpawnteeClient)
        client.has_capacity.return_value = True  # CVM says yes...
        cluster.cvms = {"cvm1": CVMInfo("cvm1", "reg", client)}
        cluster.head_id = "cvm1"

        # Assign 2 tasks to head → hits threshold
        cluster.task_client_map = {"task-1": "cvm1", "task-2": "cvm1"}

        with pytest.raises(PhalaClusterError, match="PHALA_API_KEY"):
            cluster.ensure_capacity()

    def test_cvm_safety_net_triggers_provision(self):
        """When CVM reports no capacity, provision even if model count is below threshold."""
        cluster = PhalaCluster(
            cluster_name="test",
            instance_type="tdx.large",  # max_per_cvm=7, threshold=5
            memory_per_model_mb=1024,
        )
        cluster.phala_api_key = ""
        client = MagicMock(spec=SpawnteeClient)
        client.has_capacity.return_value = False  # CVM says no!
        cluster.cvms = {"cvm1": CVMInfo("cvm1", "reg", client)}
        cluster.head_id = "cvm1"

        # Only 1 task, under threshold, but CVM reports full
        cluster.task_client_map = {"task-1": "cvm1"}

        with pytest.raises(PhalaClusterError, match="PHALA_API_KEY"):
            cluster.ensure_capacity()

    def test_global_cap_raises_error(self):
        """When total models >= max_models, refuse new models."""
        cluster = PhalaCluster(
            cluster_name="test",
            instance_type="tdx.medium",
            memory_per_model_mb=1024,
            max_models=3,
        )
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
        cluster = PhalaCluster(
            cluster_name="",
            instance_type="tdx.xlarge",  # max_per_cvm=15
            memory_per_model_mb=1024,
            max_models=0,
        )
        client = MagicMock(spec=SpawnteeClient)
        client.has_capacity.return_value = True
        cluster.cvms = {"cvm1": CVMInfo("cvm1", "reg", client)}
        cluster.head_id = "cvm1"

        # 10 tasks, no global cap
        cluster.task_client_map = {f"t{i}": "cvm1" for i in range(10)}

        # Under per-CVM threshold (12), has capacity → no provision
        cluster.ensure_capacity()

    def test_model_counts(self):
        """Test total_running_models and head_model_count."""
        cluster = PhalaCluster(cluster_name="", instance_type="tdx.medium")
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
