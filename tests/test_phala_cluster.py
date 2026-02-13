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
from model_orchestrator.infrastructure.phala._client import SpawnteeClient, SpawnteeClientError


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


class TestDiscoveryFallback:
    """Test CVM discovery using fallback URLs (no Phala API)."""

    def test_discover_single_cvm(self, mock_client_factory):
        cluster = PhalaCluster(
            cluster_name="",
            fallback_urls=["https://abc123-<model-port>.dstack-pha-prod10.phala.network"],
        )

        mock_client = mock_client_factory(mode="registry+runner", has_capacity=True)
        with patch.object(SpawnteeClient, "__new__", return_value=mock_client):
            cluster.discover()

        assert len(cluster.cvms) == 1
        assert cluster.head_id is not None

    def test_discover_no_urls(self):
        cluster = PhalaCluster(cluster_name="", fallback_urls=[])
        cluster.discover()
        assert len(cluster.cvms) == 0
        assert cluster.head_id is None

    def test_unreachable_cvm_skipped(self):
        cluster = PhalaCluster(
            cluster_name="",
            fallback_urls=["https://dead-<model-port>.example.com"],
        )

        mock_client = MagicMock(spec=SpawnteeClient)
        mock_client.health.side_effect = SpawnteeClientError("Connection refused")
        with patch.object(SpawnteeClient, "__new__", return_value=mock_client):
            cluster.discover()

        assert len(cluster.cvms) == 0


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


class TestEnsureCapacity:
    """Test capacity checking and provisioning trigger."""

    def test_head_has_capacity_no_action(self):
        cluster = PhalaCluster(cluster_name="")
        client = MagicMock(spec=SpawnteeClient)
        client.has_capacity.return_value = True
        cluster.cvms = {"cvm1": CVMInfo("cvm1", "reg", client)}
        cluster.head_id = "cvm1"

        # Should not raise or provision
        cluster.ensure_capacity()
        client.has_capacity.assert_called_once()

    def test_head_full_no_api_key_raises(self):
        cluster = PhalaCluster(cluster_name="test")
        cluster.phala_api_key = ""
        client = MagicMock(spec=SpawnteeClient)
        client.has_capacity.return_value = False
        cluster.cvms = {"cvm1": CVMInfo("cvm1", "reg", client)}
        cluster.head_id = "cvm1"

        with pytest.raises(PhalaClusterError, match="PHALA_API_KEY"):
            cluster.ensure_capacity()

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
