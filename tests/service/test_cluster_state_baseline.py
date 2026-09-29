import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from kubernetes.client.rest import ApiException

from sregym.service.cluster_state import ClusterStateManager


@pytest.fixture
def manager():
    manager = ClusterStateManager.__new__(ClusterStateManager)
    manager.baseline = None
    for name in ("core_v1", "rbac_v1", "storage_v1", "apiextensions_v1", "admission_v1"):
        setattr(manager, name, MagicMock())
    for client_name, method_name in (
        ("core_v1", "list_namespace"),
        ("rbac_v1", "list_cluster_role"),
        ("rbac_v1", "list_cluster_role_binding"),
        ("core_v1", "list_persistent_volume"),
        ("storage_v1", "list_storage_class"),
        ("apiextensions_v1", "list_custom_resource_definition"),
        ("admission_v1", "list_validating_webhook_configuration"),
        ("admission_v1", "list_mutating_webhook_configuration"),
        ("core_v1", "list_node"),
    ):
        getattr(getattr(manager, client_name), method_name).return_value = SimpleNamespace(items=[])
    return manager


@pytest.mark.parametrize(
    ("client", "method", "call_number"),
    [
        ("core_v1", "list_namespace", 1),
        ("rbac_v1", "list_cluster_role", 1),
        ("rbac_v1", "list_cluster_role_binding", 1),
        ("core_v1", "list_persistent_volume", 1),
        ("storage_v1", "list_storage_class", 1),
        ("apiextensions_v1", "list_custom_resource_definition", 1),
        ("admission_v1", "list_validating_webhook_configuration", 1),
        ("admission_v1", "list_mutating_webhook_configuration", 1),
        ("core_v1", "list_node", 1),
        ("core_v1", "list_node", 2),
        ("core_v1", "read_namespaced_config_map", 1),
    ],
)
def test_failed_baseline_read_is_not_saved(manager, tmp_path, client, method, call_number):
    read = getattr(getattr(manager, client), method)
    failure = ApiException(status=503)
    read.side_effect = failure if call_number == 1 else [SimpleNamespace(items=[]), failure]
    path = tmp_path / "baseline.json"

    with pytest.raises(ApiException) as exc_info:
        manager.save_baseline_state(path)

    assert exc_info.value is failure
    assert manager.baseline is None
    assert not path.exists()


def test_empty_successful_reads_remain_valid(manager, tmp_path):
    manager.core_v1.read_namespaced_config_map.side_effect = ApiException(status=404)
    path = tmp_path / "baseline.json"

    manager.save_baseline_state(path)

    assert json.loads(path.read_text())["cluster_roles"] == []
    assert manager.load_baseline_state(path)
