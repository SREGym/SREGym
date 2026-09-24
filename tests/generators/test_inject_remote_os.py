import json

import pytest

from sregym.generators.fault.inject_remote_os import RemoteOSFaultInjector


class FakeKubectl:
    def __init__(self, nodes):
        self.nodes = nodes

    def exec_command(self, command):
        if command == "kubectl get nodes -o json":
            return json.dumps({"items": self.nodes})
        raise AssertionError(f"unexpected command: {command}")


def _node(name, provider_id="", labels=None):
    return {
        "metadata": {
            "name": name,
            "labels": labels or {},
        },
        "spec": {
            "providerID": provider_id,
        },
    }


def _injector(nodes):
    injector = object.__new__(RemoteOSFaultInjector)
    injector.kubectl = FakeKubectl(nodes)
    injector.worker_info = None
    injector._is_kind = None
    return injector


def test_check_is_kind_detects_custom_named_kind_cluster():
    injector = _injector(
        [
            _node(
                "sregym-copilot-20036-control-plane",
                "kind://docker/sregym-copilot-20036/sregym-copilot-20036-control-plane",
                {"node-role.kubernetes.io/control-plane": ""},
            ),
            _node(
                "sregym-copilot-20036-worker",
                "kind://docker/sregym-copilot-20036/sregym-copilot-20036-worker",
            ),
        ]
    )

    assert injector._check_is_kind() is True


def test_get_kind_worker_containers_uses_current_context_provider_ids():
    injector = _injector(
        [
            _node(
                "sregym-copilot-20036-control-plane",
                "kind://docker/sregym-copilot-20036/sregym-copilot-20036-control-plane",
                {"node-role.kubernetes.io/control-plane": ""},
            ),
            _node(
                "sregym-copilot-20036-worker",
                "kind://docker/sregym-copilot-20036/sregym-copilot-20036-worker",
            ),
        ]
    )

    assert injector._get_kind_worker_containers() == ["sregym-copilot-20036-worker"]


def test_check_is_kind_keeps_remote_clusters_on_remote_path():
    injector = _injector(
        [
            _node("worker-1", "aws:///us-east-1a/i-1234567890abcdef0"),
            _node("worker-2", ""),
        ]
    )

    assert injector._check_is_kind() is False


def test_check_is_kind_retries_after_invalid_kubectl_output():
    injector = _injector([])
    outputs = iter(
        [
            "Unable to connect to the server: connection refused",
            json.dumps({"items": [_node("custom-worker", "kind://docker/custom/custom-worker")]}),
        ]
    )
    injector.kubectl.exec_command = lambda command: next(outputs)

    assert injector._check_is_kind() is False
    assert injector._is_kind is None
    assert injector._check_is_kind() is True


def test_recover_disk_pressure_all_skips_remote_cluster_without_inventory():
    injector = object.__new__(RemoteOSFaultInjector)
    injector._check_is_kind = lambda: False
    injector._check_remote_host = lambda: False
    injector._get_worker_node_names = lambda: pytest.fail("should not list workers without inventory")
    injector.recover_disk_pressure = lambda node_name: pytest.fail("should not recover without inventory")

    injector.recover_disk_pressure_all()
