import json
import subprocess
from types import SimpleNamespace

from sregym.conductor.oracles.calico_route_reflector_mitigation import CalicoRouteReflectorMitigationOracle


class _Problem:
    CURRENT_CONTROL_PLANE_LABEL = "node-role.kubernetes.io/control-plane"
    LEGACY_MASTER_LABEL = "node-role.kubernetes.io/master"
    ROUTE_REFLECTOR_CLUSTER_ID_ANNOTATION = "projectcalico.org/RouteReflectorClusterID"
    PROBE_NAMESPACE = "platform-checks"
    PROBE_CLIENT = "check-client"
    PROBE_SERVER = "remote-check"
    PROBE_LOCAL_SERVER = "local-check"
    namespace = "hotel-reservation"
    app = SimpleNamespace(frontend_service="frontend", frontend_port=5000)
    _app_deployment_replicas = {}


def _peer(name, selector):
    return {
        "metadata": {
            "name": name,
        },
        "spec": {
            "peerSelector": selector,
        },
    }


def _node(name, labels=None, annotations=None):
    return SimpleNamespace(
        metadata=SimpleNamespace(
            name=name,
            labels=labels or {},
            annotations=annotations or {},
        )
    )


def _oracle(peers, *, nodes):
    oracle = object.__new__(CalicoRouteReflectorMitigationOracle)
    oracle.problem = _Problem()
    oracle.core_v1 = SimpleNamespace(list_node=lambda: SimpleNamespace(items=nodes))
    oracle._run = lambda command, timeout=20: subprocess.CompletedProcess(
        command,
        0,
        stdout=json.dumps({"items": peers}),
        stderr="",
    )
    return oracle


def test_route_reflector_peer_rejects_unmatched_positive_legacy_selector():
    oracle = _oracle(
        [_peer("cluster-peer-policy", "has(node-role.kubernetes.io/master)")],
        nodes=[
            _node(
                "control-plane-0",
                {_Problem.CURRENT_CONTROL_PLANE_LABEL: ""},
                {_Problem.ROUTE_REFLECTOR_CLUSTER_ID_ANNOTATION: "244.0.0.1"},
            )
        ],
    )

    assert oracle._route_reflector_peer_selects_nodes() is False


def test_route_reflector_peer_accepts_current_control_plane_selector():
    oracle = _oracle(
        [_peer("current-route-reflectors", "has(node-role.kubernetes.io/control-plane)")],
        nodes=[
            _node(
                "control-plane-0",
                {_Problem.CURRENT_CONTROL_PLANE_LABEL: ""},
                {_Problem.ROUTE_REFLECTOR_CLUSTER_ID_ANNOTATION: "244.0.0.1"},
            )
        ],
    )

    assert oracle._route_reflector_peer_selects_nodes() is True


def test_route_reflector_peer_accepts_custom_route_reflector_selector():
    oracle = _oracle(
        [_peer("custom-route-reflectors", 'route-reflector == "true"')],
        nodes=[
            _node(
                "control-plane-0",
                {"route-reflector": "true"},
                {_Problem.ROUTE_REFLECTOR_CLUSTER_ID_ANNOTATION: "244.0.0.1"},
            )
        ],
    )

    assert oracle._route_reflector_peer_selects_nodes() is True


def test_route_reflector_peer_accepts_restored_legacy_selector():
    oracle = _oracle(
        [_peer("restored-master-route-reflectors", "has(node-role.kubernetes.io/master)")],
        nodes=[
            _node(
                "control-plane-0",
                {_Problem.LEGACY_MASTER_LABEL: ""},
                {_Problem.ROUTE_REFLECTOR_CLUSTER_ID_ANNOTATION: "244.0.0.1"},
            )
        ],
    )

    assert oracle._route_reflector_peer_selects_nodes() is True


def test_route_reflector_peer_rejects_selected_node_without_cluster_id():
    oracle = _oracle(
        [_peer("current-route-reflectors", "has(node-role.kubernetes.io/control-plane)")],
        nodes=[_node("control-plane-0", {_Problem.CURRENT_CONTROL_PLANE_LABEL: ""})],
    )

    assert oracle._route_reflector_peer_selects_nodes() is False


def test_route_reflector_peer_rejects_broad_selector_that_selects_workers():
    oracle = _oracle(
        [_peer("too-broad-route-reflectors", "has(kubernetes.io/hostname)")],
        nodes=[
            _node(
                "control-plane-0",
                {"kubernetes.io/hostname": "control-plane-0"},
                {_Problem.ROUTE_REFLECTOR_CLUSTER_ID_ANNOTATION: "244.0.0.1"},
            ),
            _node("worker-0", {"kubernetes.io/hostname": "worker-0"}),
        ],
    )

    assert oracle._route_reflector_peer_selects_nodes() is False


def test_route_reflector_peer_rejects_negated_legacy_selector_mesh_bypass():
    oracle = _oracle(
        [_peer("non-master-route-reflectors", "!has(node-role.kubernetes.io/master)")],
        nodes=[
            _node(
                "control-plane-0",
                {_Problem.CURRENT_CONTROL_PLANE_LABEL: ""},
                {_Problem.ROUTE_REFLECTOR_CLUSTER_ID_ANNOTATION: "244.0.0.1"},
            )
        ],
    )

    assert oracle._route_reflector_peer_selects_nodes() is False


def test_route_reflector_peer_does_not_treat_spaced_negated_legacy_selector_as_positive():
    oracle = _oracle(
        [_peer("non-master-route-reflectors", "! has(node-role.kubernetes.io/master)")],
        nodes=[],
    )

    assert oracle._route_reflector_peer_selects_nodes() is False


def test_cross_node_probe_requires_same_node_and_cross_node_paths():
    oracle = object.__new__(CalicoRouteReflectorMitigationOracle)
    oracle.problem = _Problem()
    commands = []
    pods = {
        _Problem.PROBE_CLIENT: {"name": "client", "node": "worker-0", "ip": "10.0.0.10"},
        _Problem.PROBE_LOCAL_SERVER: {"name": "local", "node": "worker-0", "ip": "10.0.0.11"},
        _Problem.PROBE_SERVER: {"name": "server", "node": "worker-1", "ip": "10.0.1.20"},
    }

    def fake_run(command, timeout=20):
        commands.append(command)
        if "get pod -l app=" in command:
            app = command.split("app=", 1)[1].split()[0]
            pod = pods[app]
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=json.dumps(
                    {
                        "items": [
                            {
                                "metadata": {"name": pod["name"]},
                                "spec": {"nodeName": pod["node"]},
                                "status": {"podIP": pod["ip"]},
                            }
                        ]
                    }
                ),
                stderr="",
            )
        if "wget" in command:
            return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="unexpected")

    oracle._run = fake_run

    assert oracle._cross_node_probe_ok() is True
    assert any("http://10.0.0.11:8080" in command for command in commands)
    assert any("http://10.0.1.20:8080" in command for command in commands)
    assert any(f"http://{_Problem.PROBE_SERVER}:8080" in command for command in commands)


def test_cross_node_probe_rejects_colocated_cross_node_server():
    oracle = object.__new__(CalicoRouteReflectorMitigationOracle)
    oracle.problem = _Problem()
    pods = {
        _Problem.PROBE_CLIENT: {"name": "client", "node": "worker-0", "ip": "10.0.0.10"},
        _Problem.PROBE_LOCAL_SERVER: {"name": "local", "node": "worker-0", "ip": "10.0.0.11"},
        _Problem.PROBE_SERVER: {"name": "server", "node": "worker-0", "ip": "10.0.0.20"},
    }

    def fake_run(command, timeout=20):
        app = command.split("app=", 1)[1].split()[0]
        pod = pods[app]
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(
                {
                    "items": [
                        {
                            "metadata": {"name": pod["name"]},
                            "spec": {"nodeName": pod["node"]},
                            "status": {"podIP": pod["ip"]},
                        }
                    ]
                }
            ),
            stderr="",
        )

    oracle._run = fake_run

    assert oracle._cross_node_probe_ok() is False


def test_app_replicas_not_reduced_rejects_scale_down():
    oracle = object.__new__(CalicoRouteReflectorMitigationOracle)
    oracle.problem = SimpleNamespace(_app_deployment_replicas={"frontend": 2})
    oracle.apps_v1 = SimpleNamespace(
        list_namespaced_deployment=lambda namespace: SimpleNamespace(
            items=[
                SimpleNamespace(
                    metadata=SimpleNamespace(name="frontend"),
                    spec=SimpleNamespace(replicas=1),
                )
            ]
        )
    )

    assert oracle._app_replicas_not_reduced("hotel-reservation") is False
