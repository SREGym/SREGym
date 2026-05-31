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

    def exec_command(self, command):
        self.commands.append(command)
        response = self.responses.get(command)
        if response is None:
            return "Error from server (NotFound): resource not found"
        return response


def _conductor(fake_kubectl, monkeypatch):
    conductor = object.__new__(Conductor)
    conductor.logger = SimpleNamespace(
        info=lambda *args, **kwargs: None,
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
    assert "kubectl annotate node control-plane-0 sregym.io/calico-route-reflector-label-drift-" in fake.commands
    assert "kubectl -n kube-system rollout restart ds/calico-node" in fake.commands


def test_calico_route_reflector_global_cleanup_reenables_mesh_when_bgpconfig_preexisted(monkeypatch):
    problem = CalicoRouteReflectorLabelDriftHotelReservation
    labels = {problem.PROBLEM_LABEL_KEY: problem.PROBLEM_LABEL_VALUE}
    fake = _FakeKubeCtl(
        {
            "kubectl get nodes -o json": _nodes(_node("control-plane-0", {problem.NODE_MARKER_ANNOTATION: "true"})),
            f"kubectl get bgppeer {problem.BGP_PEER_NAME} -o json": _json_resource(labels, name=problem.BGP_PEER_NAME),
            "kubectl get bgpconfiguration default -o json": _json_resource(name="default"),
        }
    )
    conductor = _conductor(fake, monkeypatch)

    conductor._fix_calico_route_reflector_label_drift()

    assert "kubectl delete bgpconfiguration default --ignore-not-found" not in fake.commands
    assert any(
        command.startswith("kubectl patch bgpconfiguration default --type=merge")
        and '"nodeToNodeMeshEnabled": true' in command
        for command in fake.commands
    )
