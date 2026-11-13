from types import SimpleNamespace
from unittest.mock import Mock

import pytest


class StubKubeCtl:
    """Just enough of KubeCtl for constructing apps and problems offline."""

    def __init__(self):
        self.commands = []

    def exec_command(self, command, input_data=None):
        self.commands.append(command)
        # `kubectl get namespace ... -o name` -> pretend it exists.
        return "namespace/exists"

    def exec_command_checked(self, command, input_data=None, timeout=None):
        self.commands.append(command)
        return ""


@pytest.fixture
def offline_cluster(monkeypatch):
    """Construct Incident Arena apps/problems without a kubeconfig."""
    import sregym.service.apps.components.incident_arena as app_base

    monkeypatch.setattr(app_base, "KubeCtl", StubKubeCtl)
    return StubKubeCtl


@pytest.fixture
def offline_port_cluster(monkeypatch):
    """Stub the shared client, including constructors reused by fault ports."""
    import json

    from kubernetes import client

    from sregym.service.kubectl import KubeCtl

    def initialize(kube):
        kube.core_v1_api = Mock()
        kube.apps_v1_api = Mock()
        kube.custom_objects_api = Mock()
        kube.core_v1_api.list_node.return_value = SimpleNamespace(
            items=[client.V1Node(metadata=client.V1ObjectMeta(name="worker", labels={}))]
        )
        kube.core_v1_api.list_pod_for_all_namespaces.return_value = client.V1PodList(items=[])

    def execute(kube, command, *args, **kwargs):
        if "get nodes" in command:
            if "json" in command:
                return json.dumps(
                    {"items": [{"metadata": {"name": "worker", "labels": {}}, "spec": {"providerID": "kind://worker"}}]}
                )
            return "worker Ready <none> 1d v1.32.1"
        return "namespace/exists"

    monkeypatch.setattr(KubeCtl, "__init__", initialize)
    monkeypatch.setattr(KubeCtl, "exec_command", execute)
    monkeypatch.setattr(KubeCtl, "exec_command_checked", execute)
