import json
from types import SimpleNamespace

import sregym.conductor.conductor as conductor_module
from sregym.conductor.conductor import Conductor
from sregym.conductor.problems.calico_route_reflector_label_drift import (
    CalicoRouteReflectorLabelDriftHotelReservation,
)


class _FakeKubeCtl:
    def __init__(self, responses):
        self.responses = responses
        self.commands = []
        self.inputs = {}

    def exec_command(self, command, input_data=None):
        self.commands.append(command)
        if input_data is not None:
            self.inputs[command] = input_data
        response = self.responses.get(command)
        if response is None:
            return "Error from server (NotFound): resource not found"
        return response


def _conductor(fake_kubectl, monkeypatch):
    conductor = object.__new__(Conductor)
    conductor.logger = SimpleNamespace(
        info=lambda *args, **kwargs: None,
        error=lambda *args, **kwargs: None,
        warning=lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(conductor_module, "KubeCtl", lambda: fake_kubectl)
    return conductor


def _json_resource(labels=None, annotations=None, name="resource"):
    return json.dumps(
        {
            "metadata": {
                "name": name,
                "labels": labels or {},
                "annotations": annotations or {},
            }
        }
    )


def _nodes(*nodes):
    return json.dumps({"items": list(nodes)})


def _bgppeers(*names):
    return json.dumps({"items": [{"metadata": {"name": name}} for name in names]})


def _node(name, annotations=None):
    return {
        "metadata": {
            "name": name,
            "annotations": annotations or {},
        }
    }


def test_calico_route_reflector_global_cleanup_is_noop_without_problem_markers(monkeypatch):
    problem = CalicoRouteReflectorLabelDriftHotelReservation
    fake = _FakeKubeCtl(
        {
            "kubectl get nodes -o json": _nodes(_node("control-plane-0")),
            f"kubectl get namespace {problem.PROBE_NAMESPACE} -o json": _json_resource(name=problem.PROBE_NAMESPACE),
        }
    )
    conductor = _conductor(fake, monkeypatch)

    conductor._fix_calico_route_reflector_label_drift()

    assert not any(command.startswith("kubectl delete") for command in fake.commands)
    assert not any("rollout restart ds/calico-node" in command for command in fake.commands)


def test_calico_route_reflector_global_cleanup_removes_problem_owned_state(monkeypatch):
    problem = CalicoRouteReflectorLabelDriftHotelReservation
    labels = {problem.PROBLEM_LABEL_KEY: problem.PROBLEM_LABEL_VALUE}
    fake = _FakeKubeCtl(
        {
            "kubectl get nodes -o json": _nodes(_node("control-plane-0", {problem.NODE_MARKER_ANNOTATION: "true"})),
            f"kubectl get bgppeer {problem.BGP_PEER_NAME} -o json": _json_resource(labels, name=problem.BGP_PEER_NAME),
            "kubectl get bgppeers -o json": _bgppeers(problem.BGP_PEER_NAME),
            "kubectl get bgpconfiguration default -o json": _json_resource(labels, name="default"),
            f"kubectl get namespace {problem.PROBE_NAMESPACE} -o json": _json_resource(
                labels, name=problem.PROBE_NAMESPACE
            ),
        }
    )
    conductor = _conductor(fake, monkeypatch)

    conductor._fix_calico_route_reflector_label_drift()

    assert f"kubectl delete namespace {problem.PROBE_NAMESPACE} --ignore-not-found" in fake.commands
    assert f"kubectl delete bgppeer {problem.BGP_PEER_NAME} --ignore-not-found" in fake.commands
    assert "kubectl delete bgpconfiguration default --ignore-not-found" in fake.commands
    assert "kubectl label node control-plane-0 node-role.kubernetes.io/master-" in fake.commands
    assert "kubectl annotate node control-plane-0 projectcalico.org/RouteReflectorClusterID-" in fake.commands
    assert f"kubectl annotate node control-plane-0 {problem.NODE_MARKER_ANNOTATION}-" in fake.commands
    assert "kubectl -n kube-system rollout restart ds/calico-node" in fake.commands


def test_calico_route_reflector_global_cleanup_does_not_guess_mesh_when_snapshot_is_missing(monkeypatch):
    problem = CalicoRouteReflectorLabelDriftHotelReservation
    labels = {problem.PROBLEM_LABEL_KEY: problem.PROBLEM_LABEL_VALUE}
    fake = _FakeKubeCtl(
        {
            "kubectl get nodes -o json": _nodes(_node("control-plane-0", {problem.NODE_MARKER_ANNOTATION: "true"})),
            f"kubectl get bgppeer {problem.BGP_PEER_NAME} -o json": _json_resource(labels, name=problem.BGP_PEER_NAME),
            "kubectl get bgppeers -o json": _bgppeers(problem.BGP_PEER_NAME),
            "kubectl get bgpconfiguration default -o json": _json_resource(name="default"),
        }
    )
    conductor = _conductor(fake, monkeypatch)

    conductor._fix_calico_route_reflector_label_drift()

    assert "kubectl delete bgpconfiguration default --ignore-not-found" not in fake.commands
    assert not any(
        command.startswith("kubectl patch bgpconfiguration default --type=merge") for command in fake.commands
    )


def test_calico_route_reflector_global_cleanup_restores_persisted_snapshot(monkeypatch):
    problem = CalicoRouteReflectorLabelDriftHotelReservation
    original_bgp = {
        "apiVersion": "crd.projectcalico.org/v1",
        "kind": "BGPConfiguration",
        "metadata": {"name": "default"},
        "spec": {"nodeToNodeMeshEnabled": True, "asNumber": 64512},
    }
    state_data = {
        problem.STATE_CONFIG_PREEXISTED_KEY: json.dumps(True),
        problem.STATE_CONFIGURATION_KEY: json.dumps(original_bgp),
        problem.STATE_BGP_PEERS_KEY: json.dumps(["preexisting-peer"]),
        problem.STATE_PRIMARY_NODE_KEY: "control-plane-0",
        problem.STATE_NODE_LABEL_PREEXISTED_KEY: json.dumps(True),
        problem.STATE_NODE_ANNOTATION_PREEXISTED_KEY: json.dumps(True),
        problem.STATE_NODE_ANNOTATION_VALUE_KEY: "244.0.0.9",
    }
    fake = _FakeKubeCtl(
        {
            "kubectl get nodes -o json": _nodes(_node("control-plane-0")),
            "kubectl get bgppeers -o json": _bgppeers("preexisting-peer", problem.BGP_PEER_NAME, "agent-created-peer"),
            f"kubectl -n {problem.STATE_NAMESPACE} get configmap {problem.STATE_CONFIGMAP_NAME} -o json": json.dumps(
                {"metadata": {"name": problem.STATE_CONFIGMAP_NAME}, "data": state_data}
            ),
        }
    )
    conductor = _conductor(fake, monkeypatch)

    conductor._fix_calico_route_reflector_label_drift()

    assert "kubectl apply -f -" in fake.commands
    assert json.loads(fake.inputs["kubectl apply -f -"])["spec"] == {
        "nodeToNodeMeshEnabled": True,
        "asNumber": 64512,
    }
    assert "kubectl label node control-plane-0 node-role.kubernetes.io/master= --overwrite" in fake.commands
    assert (
        "kubectl annotate node control-plane-0 projectcalico.org/RouteReflectorClusterID=244.0.0.9 --overwrite"
        in fake.commands
    )
    assert (
        f"kubectl -n {problem.STATE_NAMESPACE} delete configmap {problem.STATE_CONFIGMAP_NAME} --ignore-not-found"
        in fake.commands
    )
    assert f"kubectl delete bgppeer {problem.BGP_PEER_NAME} --ignore-not-found" in fake.commands
    assert "kubectl delete bgppeer agent-created-peer --ignore-not-found" in fake.commands
    assert "kubectl delete bgppeer preexisting-peer --ignore-not-found" not in fake.commands


def test_calico_route_reflector_global_cleanup_keeps_unowned_support_namespace(monkeypatch):
    problem = CalicoRouteReflectorLabelDriftHotelReservation
    state_data = {
        problem.STATE_CONFIG_PREEXISTED_KEY: json.dumps(False),
        problem.STATE_BGP_PEERS_KEY: json.dumps([]),
    }
    fake = _FakeKubeCtl(
        {
            "kubectl get nodes -o json": _nodes(),
            "kubectl get bgppeers -o json": _bgppeers(problem.BGP_PEER_NAME),
            f"kubectl get namespace {problem.PROBE_NAMESPACE} -o json": _json_resource(name=problem.PROBE_NAMESPACE),
            f"kubectl -n {problem.STATE_NAMESPACE} get configmap {problem.STATE_CONFIGMAP_NAME} -o json": json.dumps(
                {"metadata": {"name": problem.STATE_CONFIGMAP_NAME}, "data": state_data}
            ),
        }
    )
    conductor = _conductor(fake, monkeypatch)

    conductor._fix_calico_route_reflector_label_drift()

    assert f"kubectl delete namespace {problem.PROBE_NAMESPACE} --ignore-not-found" not in fake.commands
    assert f"kubectl delete bgppeer {problem.BGP_PEER_NAME} --ignore-not-found" in fake.commands


def test_fix_kubernetes_keeps_kubelet_eviction_global_cleanup(monkeypatch):
    fake = _FakeKubeCtl({})
    conductor = _conductor(fake, monkeypatch)
    conductor.kubectl = fake
    conductor.dm_flakey_manager = SimpleNamespace(teardown_openebs_dm_flakey_infrastructure=lambda: None)
    remote_calls = []
    calico_cleanup_calls = []

    class FakeRemoteOSFaultInjector:
        def recover_kubelet_crash(self):
            remote_calls.append("recover_kubelet_crash")

        def recover_disk_pressure_all(self):
            remote_calls.append("recover_disk_pressure_all")

    class FakeVirtualizationFaultInjector:
        def __init__(self, namespace):
            self.namespace = namespace

        def recover_daemon_set_image_replacement(self, daemon_set_name, original_image):
            pass

        def recover_all_nxdomain_templates(self):
            pass

    monkeypatch.setattr(conductor_module, "RemoteOSFaultInjector", FakeRemoteOSFaultInjector)
    monkeypatch.setattr(conductor_module, "VirtualizationFaultInjector", FakeVirtualizationFaultInjector)
    monkeypatch.setattr(
        Conductor,
        "_fix_calico_route_reflector_label_drift",
        lambda self: calico_cleanup_calls.append(True),
    )

    conductor.fix_kubernetes()

    assert remote_calls == ["recover_kubelet_crash", "recover_disk_pressure_all"]
    assert (
        "kubectl delete pods --all-namespaces "
        "--field-selector=status.phase=Failed,metadata.namespace!=astronomy-shop "
        "--ignore-not-found=true"
    ) in fake.commands
    assert calico_cleanup_calls == [True]
