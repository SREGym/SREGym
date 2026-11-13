import json
from unittest.mock import Mock

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


def _ready_node(name, ready="True", *, cordoned=False, labels=None):
    node = _node(name, labels=labels)
    node["spec"]["unschedulable"] = cordoned
    node["status"] = {"conditions": [] if ready is None else [{"type": "Ready", "status": ready}]}
    return node


@pytest.fixture
def polling_clock(monkeypatch):
    clock = Mock()
    now = [0]

    def sleep(seconds):
        now[0] += seconds

    clock.sleep.side_effect = sleep
    monkeypatch.setattr("sregym.generators.fault.inject_remote_os.time.sleep", clock.sleep)
    monkeypatch.setattr("sregym.generators.fault.inject_remote_os.time.monotonic", lambda: now[0])
    monkeypatch.setattr("sregym.generators.fault.inject_remote_os.time.time", lambda: now[0])
    return clock


@pytest.mark.parametrize("single", [False, True])
def test_ready_poll_accepts_a_cordoned_node(polling_clock, single):
    injector = _injector([_ready_node("worker", cordoned=True)])
    if single:
        result = injector._wait_for_single_node("worker", timeout=1)
    else:
        result = injector._wait_for_worker_nodes("Ready", timeout=1)
    assert result is True
    polling_clock.sleep.assert_not_called()


@pytest.mark.parametrize("condition", ["False", "Unknown"])
@pytest.mark.parametrize("cordoned", [False, True])
def test_not_ready_poll_accepts_false_and_unknown(polling_clock, condition, cordoned):
    injector = _injector([_ready_node("worker", condition, cordoned=cordoned)])
    assert injector._wait_for_worker_nodes("NotReady", timeout=1) is True
    polling_clock.sleep.assert_not_called()


@pytest.mark.parametrize(
    "labels", [{"node-role.kubernetes.io/control-plane": ""}, {"node-role.kubernetes.io/master": ""}]
)
def test_worker_poll_ignores_control_plane_readiness(polling_clock, labels):
    injector = _injector([_ready_node("control", "False", labels=labels), _ready_node("worker")])
    assert injector._wait_for_worker_nodes("Ready", timeout=1) is True


@pytest.mark.parametrize("current", [[], [_ready_node("worker", None)], [_ready_node("worker", "False")], None])
def test_ready_poll_does_not_accept_missing_or_unavailable_evidence(polling_clock, current):
    injector = _injector([])
    injector._get_node_items = Mock(side_effect=[[_ready_node("worker")], current])
    assert injector._wait_for_worker_nodes("Ready", timeout=1) is False


def test_not_ready_poll_does_not_accept_a_ready_node(polling_clock):
    injector = _injector([_ready_node("worker")])
    assert injector._wait_for_worker_nodes("NotReady", timeout=1) is False


def test_worker_poll_waits_until_every_original_worker_recovers(polling_clock):
    injector = _injector([])
    injector._get_node_items = Mock(
        side_effect=[
            [_ready_node("one"), _ready_node("two", "Unknown")],
            [_ready_node("one")],
            [_ready_node("one"), _ready_node("two", "Unknown")],
            [_ready_node("one", cordoned=True), _ready_node("two", cordoned=True)],
        ]
    )
    assert injector._wait_for_worker_nodes("Ready", timeout=15) is True
    assert polling_clock.sleep.call_count == 2


def test_worker_discovery_failure_is_not_a_successful_empty_cluster(polling_clock):
    injector = _injector([])
    injector._get_node_items = Mock(return_value=None)
    with pytest.raises(RuntimeError, match="worker nodes"):
        injector._wait_for_worker_nodes("Ready", timeout=1)
