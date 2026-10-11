from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from kubernetes.client.exceptions import ApiException

from sregym.service.agent_visibility_policy import (
    HIDDEN_LABELS,
    HIDDEN_NAMESPACES,
    NODE_MAINTENANCE_NAMESPACE,
    is_hidden_resource,
)
from sregym.service.kubectl import KubeCtl


def test_trusted_node_helper_is_private_without_changing_its_maintenance_operation():
    kubectl = object.__new__(KubeCtl)
    kubectl.core_v1_api = MagicMock()
    kubectl.core_v1_api.read_namespaced_pod.return_value = SimpleNamespace(status=SimpleNamespace(phase="Succeeded"))
    kubectl.core_v1_api.read_namespaced_pod_log.return_value = "maintenance complete"
    result = kubectl.run_node_script_pod("worker-1", "astronomy-shop", "echo maintenance", "cleanup")
    assert result == "maintenance complete"
    pod = kubectl.core_v1_api.create_namespaced_pod.call_args.kwargs["body"]
    assert kubectl.core_v1_api.create_namespaced_pod.call_args.kwargs["namespace"] == NODE_MAINTENANCE_NAMESPACE
    assert pod.spec.node_name == "worker-1"
    assert pod.spec.automount_service_account_token is False
    assert pod.spec.containers[0].command == ["sh", "-c", "echo maintenance"]
    assert pod.spec.containers[0].security_context.privileged is True
    assert pod.spec.volumes[0].host_path.path == "/"
    visible_object = {"kind": "Pod", "metadata": {"namespace": "astronomy-shop", "labels": pod.metadata.labels}}
    assert is_hidden_resource(visible_object, HIDDEN_NAMESPACES, HIDDEN_LABELS)
    # Custom display filters cannot accidentally expose a trusted helper.
    assert is_hidden_resource(visible_object, set(), {})
    assert kubectl.core_v1_api.delete_namespaced_pod.call_count == 1


@pytest.mark.parametrize("creation_status", [None, 409])
def test_trusted_helper_creates_private_namespace_and_accepts_creation_race(creation_status):
    kubectl = object.__new__(KubeCtl)
    kubectl.core_v1_api = MagicMock()
    kubectl.core_v1_api.read_namespace.side_effect = ApiException(status=404)
    if creation_status:
        kubectl.core_v1_api.create_namespace.side_effect = ApiException(status=creation_status)
    kubectl.core_v1_api.read_namespaced_pod.return_value = SimpleNamespace(status=SimpleNamespace(phase="Succeeded"))
    kubectl.core_v1_api.read_namespaced_pod_log.return_value = "ok"
    assert kubectl.run_node_script_pod("worker-1", "astronomy-shop", "echo ok", "cleanup") == "ok"
    assert kubectl.core_v1_api.create_namespace.call_args.kwargs["body"].metadata.name == NODE_MAINTENANCE_NAMESPACE
    assert kubectl.core_v1_api.create_namespaced_pod.call_args.kwargs["namespace"] == NODE_MAINTENANCE_NAMESPACE


def test_trusted_helper_namespace_failure_cannot_fall_back_to_application_namespace():
    kubectl = object.__new__(KubeCtl)
    kubectl.core_v1_api = MagicMock()
    kubectl.core_v1_api.read_namespace.side_effect = ApiException(status=403)
    with pytest.raises(ApiException):
        kubectl.run_node_script_pod("worker-1", "astronomy-shop", "echo ok", "cleanup")
    kubectl.core_v1_api.create_namespaced_pod.assert_not_called()


def test_helpers_with_same_prefix_and_node_have_distinct_owned_names():
    kubectl = object.__new__(KubeCtl)
    kubectl.core_v1_api = MagicMock()
    kubectl.core_v1_api.read_namespaced_pod.return_value = SimpleNamespace(status=SimpleNamespace(phase="Succeeded"))
    for namespace in ("first-application", "second-application"):
        kubectl.run_node_script_pod("worker-1", namespace, "true", "cleanup")
    created = [call.kwargs["body"].metadata.name for call in kubectl.core_v1_api.create_namespaced_pod.call_args_list]
    deleted = [call.kwargs["name"] for call in kubectl.core_v1_api.delete_namespaced_pod.call_args_list]
    assert len(set(created)) == 2
    assert deleted == created
    assert all(len(name) <= 63 for name in created)
    operations = [call[0] for call in kubectl.core_v1_api.method_calls]
    assert operations.index("create_namespaced_pod") < operations.index("delete_namespaced_pod")


def test_helper_creation_conflict_cannot_delete_an_existing_pod():
    kubectl = object.__new__(KubeCtl)
    kubectl.core_v1_api = MagicMock()
    kubectl.core_v1_api.create_namespaced_pod.side_effect = ApiException(status=409)
    with pytest.raises(ApiException):
        kubectl.run_node_script_pod("worker-1", "demo", "true", "cleanup")
    kubectl.core_v1_api.delete_namespaced_pod.assert_not_called()
