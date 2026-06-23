import json
import subprocess
from types import SimpleNamespace

import pytest

from sregym.conductor.problems.calico_route_reflector_label_drift import (
    CalicoRouteReflectorLabelDriftHotelReservation,
)


def _problem():
    problem = object.__new__(CalicoRouteReflectorLabelDriftHotelReservation)
    problem.original_bgp_configuration = None
    problem._bgp_config_preexisted = None
    problem._legacy_label_preexisted = None
    problem._route_reflector_annotation_preexisted = None
    problem._route_reflector_annotation_value = None
    problem.route_reflector_node = None
    problem._original_bgppeer_names = None
    problem._app_deployment_replicas = {}
    return problem


def test_agent_visible_ownership_keys_do_not_leak_harness_name():
    assert "sregym" not in CalicoRouteReflectorLabelDriftHotelReservation.PROBLEM_LABEL_KEY
    assert "sregym" not in CalicoRouteReflectorLabelDriftHotelReservation.NODE_MARKER_ANNOTATION


def test_capture_bgp_configuration_marks_missing_config_as_created_by_problem():
    problem = _problem()
    problem._run = lambda command, check=False: subprocess.CompletedProcess(
        command,
        1,
        stdout="",
        stderr='Error from server (NotFound): bgpconfigurations.crd.projectcalico.org "default" not found',
    )

    problem._capture_bgp_configuration()

    assert problem._bgp_config_preexisted is False
    assert problem.original_bgp_configuration is None


def test_capture_bgp_configuration_refuses_unknown_read_failure():
    problem = _problem()
    problem._run = lambda command, check=False: subprocess.CompletedProcess(
        command,
        124,
        stdout="",
        stderr="Timed out after 120s",
    )

    with pytest.raises(RuntimeError, match="Could not safely capture existing Calico BGPConfiguration"):
        problem._capture_bgp_configuration()


def test_cleanup_does_not_delete_bgp_configuration_when_capture_was_not_run():
    problem = _problem()
    calls = []
    problem._run = lambda command, *args, **kwargs: (
        calls.append(command)
        or subprocess.CompletedProcess(
            command,
            0,
            stdout="",
            stderr="",
        )
    )

    problem._delete_support_resources()

    assert "kubectl delete bgpconfiguration default --ignore-not-found" not in calls


def test_cleanup_deletes_bgp_configuration_only_when_problem_created_it():
    problem = _problem()
    problem._bgp_config_preexisted = False
    calls = []
    problem._run = lambda command, *args, **kwargs: (
        calls.append(command)
        or subprocess.CompletedProcess(
            command,
            0,
            stdout="",
            stderr="",
        )
    )

    problem._delete_support_resources()

    assert "kubectl delete bgpconfiguration default --ignore-not-found" in calls


def test_capture_bgp_peers_records_preexisting_names():
    problem = _problem()
    problem._run = lambda command, *args, **kwargs: subprocess.CompletedProcess(
        command,
        0,
        stdout=json.dumps(
            {
                "items": [
                    {"metadata": {"name": "preexisting-peer-a"}},
                    {"metadata": {"name": "preexisting-peer-b"}},
                ]
            }
        ),
        stderr="",
    )

    problem._capture_bgp_peers()

    assert problem._original_bgppeer_names == {"preexisting-peer-a", "preexisting-peer-b"}


def test_capture_bgp_peers_refuses_fixed_name_collision():
    problem = _problem()
    problem._run = lambda command, *args, **kwargs: subprocess.CompletedProcess(
        command,
        0,
        stdout=json.dumps({"items": [{"metadata": {"name": problem.BGP_PEER_NAME}}]}),
        stderr="",
    )

    with pytest.raises(RuntimeError, match="already exists"):
        problem._capture_bgp_peers()


def test_cleanup_deletes_bgppeers_created_after_snapshot():
    problem = _problem()
    problem._original_bgppeer_names = {"preexisting-peer"}
    calls = []

    def fake_run(command, *args, **kwargs):
        calls.append(command)
        if command == "kubectl get bgppeers -o json":
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=json.dumps(
                    {
                        "items": [
                            {"metadata": {"name": "preexisting-peer"}},
                            {"metadata": {"name": problem.BGP_PEER_NAME}},
                            {"metadata": {"name": "agent-created-peer"}},
                        ]
                    }
                ),
                stderr="",
            )
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    problem._run = fake_run

    problem._delete_support_resources()

    assert f"kubectl delete bgppeer {problem.BGP_PEER_NAME} --ignore-not-found" in calls
    assert "kubectl delete bgppeer agent-created-peer --ignore-not-found" in calls
    assert "kubectl delete bgppeer preexisting-peer --ignore-not-found" not in calls


def test_cleanup_restores_preexisting_bgp_configuration():
    problem = _problem()
    problem._bgp_config_preexisted = True
    problem.original_bgp_configuration = {
        "apiVersion": "crd.projectcalico.org/v1",
        "kind": "BGPConfiguration",
        "metadata": {"name": "default"},
        "spec": {"nodeToNodeMeshEnabled": True},
    }
    calls = []
    applied = []
    problem._run = lambda command, *args, **kwargs: (
        calls.append(command)
        or subprocess.CompletedProcess(
            command,
            0,
            stdout="",
            stderr="",
        )
    )
    problem._apply_manifest = lambda manifest: applied.append(manifest)

    problem._delete_support_resources()

    assert f"kubectl delete bgppeer {problem.BGP_PEER_NAME} --ignore-not-found" in calls
    assert json.loads(applied[0])["spec"] == {"nodeToNodeMeshEnabled": True}


def test_cleanup_does_not_remove_route_reflector_node_state_when_capture_was_not_run():
    problem = _problem()
    problem.route_reflector_node = "control-plane-0"
    calls = []
    problem._run = lambda command, *args, **kwargs: (
        calls.append(command)
        or subprocess.CompletedProcess(
            command,
            0,
            stdout="",
            stderr="",
        )
    )

    problem._delete_support_resources()

    assert all("node-role.kubernetes.io/master" not in call for call in calls)
    assert all("projectcalico.org/RouteReflectorClusterID" not in call for call in calls)


def test_cleanup_restores_preexisting_route_reflector_node_state():
    problem = _problem()
    problem.route_reflector_node = "control-plane-0"
    problem._legacy_label_preexisted = True
    problem._route_reflector_annotation_preexisted = True
    problem._route_reflector_annotation_value = "244.0.0.9"
    calls = []
    problem._run = lambda command, *args, **kwargs: (
        calls.append(command)
        or subprocess.CompletedProcess(
            command,
            0,
            stdout="node/control-plane-0 updated",
            stderr="",
        )
    )

    problem._delete_support_resources()

    assert "kubectl label node control-plane-0 node-role.kubernetes.io/master= --overwrite" in calls
    assert (
        "kubectl annotate node control-plane-0 projectcalico.org/RouteReflectorClusterID=244.0.0.9 --overwrite" in calls
    )


def test_cleanup_removes_problem_created_route_reflector_node_state():
    problem = _problem()
    problem.route_reflector_node = "control-plane-0"
    problem._legacy_label_preexisted = False
    problem._route_reflector_annotation_preexisted = False
    calls = []
    problem._run = lambda command, *args, **kwargs: (
        calls.append(command)
        or subprocess.CompletedProcess(
            command,
            0,
            stdout="node/control-plane-0 updated",
            stderr="",
        )
    )

    problem._delete_support_resources()

    assert "kubectl label node control-plane-0 node-role.kubernetes.io/master-" in calls
    assert "kubectl annotate node control-plane-0 projectcalico.org/RouteReflectorClusterID-" in calls


def test_fresh_cleanup_removes_problem_marked_calico_resources_and_node_state():
    problem = _problem()
    problem.core_v1 = SimpleNamespace(
        list_node=lambda: SimpleNamespace(
            items=[
                _node(
                    "control-plane-0",
                    {},
                    {problem.NODE_MARKER_ANNOTATION: "true"},
                )
            ]
        ),
        read_node=lambda name: _node(name, {}, {problem.NODE_MARKER_ANNOTATION: "true"}),
    )
    calls = []

    def fake_run(command, *args, **kwargs):
        calls.append(command)
        stdout = ""
        if command.startswith("kubectl get bgppeer ") or command == "kubectl get bgpconfiguration default -o json":
            stdout = json.dumps(
                {
                    "metadata": {
                        "labels": {
                            problem.PROBLEM_LABEL_KEY: problem.PROBLEM_LABEL_VALUE,
                        }
                    }
                }
            )
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    problem._run = fake_run

    problem._delete_support_resources()

    assert f"kubectl delete bgppeer {problem.BGP_PEER_NAME} --ignore-not-found" in calls
    assert "kubectl delete bgpconfiguration default --ignore-not-found" in calls
    assert "kubectl label node control-plane-0 node-role.kubernetes.io/master-" in calls
    assert "kubectl annotate node control-plane-0 projectcalico.org/RouteReflectorClusterID-" in calls
    assert f"kubectl annotate node control-plane-0 {problem.NODE_MARKER_ANNOTATION}-" in calls


def test_persist_original_state_writes_generic_configmap_snapshot():
    problem = _problem()
    problem.route_reflector_node = "control-plane-0"
    problem._bgp_config_preexisted = True
    problem.original_bgp_configuration = {
        "apiVersion": "crd.projectcalico.org/v1",
        "kind": "BGPConfiguration",
        "metadata": {"name": "default"},
        "spec": {"nodeToNodeMeshEnabled": True},
    }
    problem._legacy_label_preexisted = True
    problem._route_reflector_annotation_preexisted = True
    problem._route_reflector_annotation_value = "244.0.0.9"
    problem._original_bgppeer_names = {"preexisting-peer"}
    applied = []
    problem._apply_manifest = applied.append
    problem._run = lambda command, *args, **kwargs: subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    problem._persist_original_state()

    manifest = json.loads(applied[0])
    assert manifest["metadata"]["name"] == problem.STATE_CONFIGMAP_NAME
    assert manifest["metadata"]["namespace"] == problem.STATE_NAMESPACE
    assert manifest["metadata"]["labels"] == {problem.PROBLEM_LABEL_KEY: problem.PROBLEM_LABEL_VALUE}
    assert json.loads(manifest["data"][problem.STATE_CONFIG_PREEXISTED_KEY]) is True
    assert json.loads(manifest["data"][problem.STATE_CONFIGURATION_KEY])["spec"] == {"nodeToNodeMeshEnabled": True}
    assert json.loads(manifest["data"][problem.STATE_BGP_PEERS_KEY]) == ["preexisting-peer"]


def test_cleanup_restores_persisted_state_after_interrupted_run():
    problem = _problem()
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
    calls = []
    applied = []

    def fake_run(command, *args, **kwargs):
        calls.append(command)
        if command == f"kubectl -n {problem.STATE_NAMESPACE} get configmap {problem.STATE_CONFIGMAP_NAME} -o json":
            return subprocess.CompletedProcess(command, 0, stdout=json.dumps({"data": state_data}), stderr="")
        if command == "kubectl get bgppeers -o json":
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=json.dumps(
                    {
                        "items": [
                            {"metadata": {"name": "preexisting-peer"}},
                            {"metadata": {"name": problem.BGP_PEER_NAME}},
                        ]
                    }
                ),
                stderr="",
            )
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    problem._run = fake_run
    problem._apply_manifest = applied.append

    problem._delete_support_resources()

    assert json.loads(applied[0])["spec"] == {"nodeToNodeMeshEnabled": True, "asNumber": 64512}
    assert "kubectl label node control-plane-0 node-role.kubernetes.io/master= --overwrite" in calls
    assert (
        "kubectl annotate node control-plane-0 projectcalico.org/RouteReflectorClusterID=244.0.0.9 --overwrite" in calls
    )
    assert (
        f"kubectl -n {problem.STATE_NAMESPACE} delete configmap {problem.STATE_CONFIGMAP_NAME} --ignore-not-found"
        in calls
    )
    assert f"kubectl delete bgppeer {problem.BGP_PEER_NAME} --ignore-not-found" in calls


def test_inject_fault_cleans_residue_before_capturing_baseline():
    problem = _problem()
    events = []
    problem._calico_available = lambda: True
    problem._calico_bgp_dataplane_available = lambda: True
    problem._select_nodes = lambda: events.append("select")
    problem._delete_support_resources = lambda: events.append("cleanup")
    problem._capture_bgp_configuration = lambda: events.append("capture_bgp")
    problem._capture_bgp_peers = lambda: events.append("capture_peers")
    problem._capture_route_reflector_node_state = lambda: events.append("capture_node")
    problem._persist_original_state = lambda: events.append("persist")
    problem._capture_app_deployment_replicas = lambda: events.append("capture_app")
    problem._prepare_cross_node_app_path = lambda: events.append("prepare_app")
    problem._deploy_probe = lambda: events.append("deploy_probe")
    problem._configure_healthy_route_reflectors_with_legacy_label = lambda: events.append("configure")
    problem._remove_legacy_route_reflector_label = lambda: events.append("remove_label")
    problem._wait_for_probe = lambda expect_success, timeout: events.append(f"probe_{expect_success}")

    problem.inject_fault()

    assert events.index("cleanup") < events.index("capture_bgp")
    assert events.index("capture_bgp") < events.index("capture_peers")
    assert events.index("persist") < events.index("configure")


def test_probe_succeeds_treats_missing_client_pod_as_not_ready():
    problem = _problem()
    problem._run = lambda command, *args, **kwargs: subprocess.CompletedProcess(
        command,
        1,
        stdout="",
        stderr="array index out of bounds",
    )

    assert problem._probe_succeeds() is False


def test_probe_succeeds_requires_same_node_and_cross_node_paths():
    problem = _problem()
    problem._same_node_pod_ip_probe_succeeds = lambda: True
    problem._cross_node_pod_ip_probe_succeeds = lambda: True
    problem._cross_node_service_probe_succeeds = lambda: False

    assert problem._probe_succeeds() is False


def test_probe_fault_observed_requires_local_success_and_cross_node_failures():
    problem = _problem()
    problem._same_node_pod_ip_probe_succeeds = lambda: True
    problem._cross_node_pod_ip_probe_succeeds = lambda: False
    problem._cross_node_service_probe_succeeds = lambda: False

    assert problem._probe_fault_observed() is True


def test_deploy_probe_labels_support_namespace():
    problem = _problem()
    problem.worker_nodes = ["worker-0", "worker-1"]
    manifests = []
    problem._apply_manifest = manifests.append
    problem._run = lambda command, *args, **kwargs: subprocess.CompletedProcess(
        command,
        1 if command == f"kubectl get namespace {problem.PROBE_NAMESPACE} -o json" else 0,
        stdout="",
        stderr="not found" if command == f"kubectl get namespace {problem.PROBE_NAMESPACE} -o json" else "",
    )

    problem._deploy_probe()

    assert f"{problem.PROBLEM_LABEL_KEY}: {problem.PROBLEM_LABEL_VALUE}" in manifests[0]


def test_deploy_probe_refuses_unowned_existing_support_namespace():
    problem = _problem()
    problem.worker_nodes = ["worker-0", "worker-1"]
    problem._apply_manifest = lambda manifest: pytest.fail("should not apply probe manifest")

    def fake_run(command, *args, **kwargs):
        if command == f"kubectl get namespace {problem.PROBE_NAMESPACE} -o json":
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=json.dumps({"metadata": {"labels": {}}}),
                stderr="",
            )
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    problem._run = fake_run

    with pytest.raises(RuntimeError, match="already exists without"):
        problem._deploy_probe()


def test_cleanup_does_not_delete_unowned_probe_namespace():
    problem = _problem()
    calls = []

    def fake_run(command, *args, **kwargs):
        calls.append(command)
        if command == f"kubectl get namespace {problem.PROBE_NAMESPACE} -o json":
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=json.dumps({"metadata": {"labels": {}}}),
                stderr="",
            )
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    problem._run = fake_run

    problem._delete_support_resources()

    assert f"kubectl delete namespace {problem.PROBE_NAMESPACE} --ignore-not-found" not in calls


def test_select_nodes_rejects_multiple_control_planes():
    problem = _problem()
    problem.core_v1 = SimpleNamespace(
        list_node=lambda: SimpleNamespace(
            items=[
                _node("control-plane-0", {problem.CURRENT_CONTROL_PLANE_LABEL: ""}),
                _node("control-plane-1", {problem.CURRENT_CONTROL_PLANE_LABEL: ""}),
                _node("worker-0", {}),
                _node("worker-1", {}),
            ]
        )
    )

    with pytest.raises(RuntimeError, match="requires exactly one control-plane node"):
        problem._select_nodes()


def test_calico_bgp_dataplane_preflight_uses_calico_node_bird_status():
    problem = _problem()
    commands = []

    def fake_run(command, *args, **kwargs):
        commands.append(command)
        if command.startswith("kubectl -n kube-system get pod"):
            return subprocess.CompletedProcess(command, 0, stdout="calico-node-abc", stderr="")
        if "birdcl show status" in command:
            return subprocess.CompletedProcess(command, 0, stdout="BIRD ready", stderr="")
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="unexpected command")

    problem._run = fake_run

    assert problem._calico_bgp_dataplane_available() is True
    assert commands[-1] == "kubectl -n kube-system exec calico-node-abc -c calico-node -- birdcl show status"


def _node(name, labels, annotations=None):
    return SimpleNamespace(
        metadata=SimpleNamespace(
            name=name,
            labels=labels,
            annotations=annotations or {},
        )
    )
