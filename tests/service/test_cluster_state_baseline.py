import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from kubernetes.client.rest import ApiException

from sregym.service.cluster_state import ClusterBaseline, ClusterStateManager


@pytest.fixture
def manager():
    manager = ClusterStateManager.__new__(ClusterStateManager)
    manager.baseline = None
    for name in ("core_v1", "rbac_v1", "storage_v1", "apiextensions_v1", "admission_v1"):
        setattr(manager, name, MagicMock())
    for client_name, method_name in (
        ("core_v1", "read_namespace"),
        ("core_v1", "list_namespace"),
        ("rbac_v1", "list_cluster_role"),
        ("rbac_v1", "list_cluster_role_binding"),
        ("core_v1", "list_persistent_volume"),
        ("storage_v1", "list_storage_class"),
        ("apiextensions_v1", "list_custom_resource_definition"),
        ("admission_v1", "list_validating_webhook_configuration"),
        ("admission_v1", "list_mutating_webhook_configuration"),
        ("admission_v1", "list_validating_admission_policy"),
        ("admission_v1", "list_validating_admission_policy_binding"),
        ("core_v1", "list_node"),
    ):
        getattr(getattr(manager, client_name), method_name).return_value = SimpleNamespace(items=[])
    manager.core_v1.read_namespace.return_value = SimpleNamespace(metadata=SimpleNamespace(uid="cluster-1"))
    return manager


@pytest.mark.parametrize(
    ("client", "method", "call_number"),
    [
        ("core_v1", "read_namespace", 1),
        ("core_v1", "list_namespace", 1),
        ("rbac_v1", "list_cluster_role", 1),
        ("rbac_v1", "list_cluster_role_binding", 1),
        ("core_v1", "list_persistent_volume", 1),
        ("storage_v1", "list_storage_class", 1),
        ("apiextensions_v1", "list_custom_resource_definition", 1),
        ("admission_v1", "list_validating_webhook_configuration", 1),
        ("admission_v1", "list_mutating_webhook_configuration", 1),
        ("admission_v1", "list_validating_admission_policy", 1),
        ("admission_v1", "list_validating_admission_policy_binding", 1),
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

    assert json.loads(path.read_text())["cluster_uid"] == "cluster-1"
    assert json.loads(path.read_text())["cluster_roles"] == []
    assert manager.load_baseline_state(path)


def test_admission_policy_baseline_round_trip():
    baseline = ClusterBaseline(
        validating_admission_policies={"existing-policy"},
        validating_admission_policy_bindings={"existing-binding"},
    )
    assert ClusterBaseline.from_json(baseline.to_json()) == baseline
    assert baseline.to_dict()["validating_admission_policies"] == ["existing-policy"]


def test_legacy_baseline_preserves_preexisting_admission_policies(manager, tmp_path):
    manager.admission_v1.list_validating_admission_policy.return_value.items = [
        SimpleNamespace(metadata=SimpleNamespace(name="existing-policy"))
    ]
    path = tmp_path / "baseline.json"
    path.write_text(json.dumps({"cluster_uid": "cluster-1"}))
    assert manager.load_baseline_state(path)
    assert manager.baseline.validating_admission_policies == {"existing-policy"}


def test_baseline_without_cluster_identity_is_rejected(manager, tmp_path):
    path = tmp_path / "baseline.json"
    path.write_text("{}")

    assert not manager.load_baseline_state(path)
    assert manager.baseline is None


def test_baseline_from_another_cluster_is_rejected(manager, tmp_path):
    path = tmp_path / "baseline.json"
    path.write_text(json.dumps({"cluster_uid": "old-cluster"}))

    assert not manager.load_baseline_state(path)
    assert manager.baseline is None


@pytest.mark.parametrize("resource", ["policies", "bindings"])
@pytest.mark.parametrize("status", [403, 404, 503])
def test_admission_policy_api_errors(manager, resource, status):
    method = "list_validating_admission_policy" + ("_binding" if resource == "bindings" else "")
    getattr(manager.admission_v1, method).side_effect = ApiException(status=status)
    if status == 404:
        assert manager._get_admission_policy_names(resource) == set()
    else:
        with pytest.raises(ApiException):
            manager._get_admission_policy_names(resource)


def test_reconcile_removes_new_policies_before_namespaces_and_preserves_baseline(manager):
    manager.core_v1.read_namespaced_config_map.side_effect = ApiException(status=404)
    manager.capture_baseline()
    manager.baseline.validating_admission_policies = {"existing-policy"}
    manager.baseline.validating_admission_policy_bindings = {"existing-binding"}
    for method, names in (
        ("list_validating_admission_policy", ["existing-policy", "new-policy"]),
        ("list_validating_admission_policy_binding", ["existing-binding", "new-binding"]),
    ):
        getattr(manager.admission_v1, method).return_value.items = [
            SimpleNamespace(metadata=SimpleNamespace(name=name)) for name in names
        ]
    manager.kubectl = MagicMock()
    manager.kubectl.gc_orphan_localpv_dirs.return_value = {}
    changes = manager.reconcile_to_baseline()
    manager.admission_v1.delete_validating_admission_policy.assert_called_once_with(name="new-policy")
    manager.admission_v1.delete_validating_admission_policy_binding.assert_called_once_with(name="new-binding")
    deletes = [call[0] for call in manager.admission_v1.method_calls if call[0].startswith("delete_")]
    assert deletes == ["delete_validating_admission_policy_binding", "delete_validating_admission_policy"]
    assert changes["validating_admission_policies_deleted"] == ["new-policy"]
