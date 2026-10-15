"""MitigationOracle's baseline covers the objects the app depends on, not only
its Deployments: deleting a Service, PersistentVolumeClaim, ConfigMap or
StatefulSet leaves every pod Ready, so only the baseline can catch it."""

from types import SimpleNamespace

import pytest

from sregym.conductor.oracles import mitigation
from sregym.conductor.oracles.mitigation import MitigationOracle


@pytest.fixture(autouse=True)
def _fast_rollout_settle(monkeypatch):
    monkeypatch.setattr(mitigation, "_ROLLOUT_SETTLE_SECONDS", 0.05)
    monkeypatch.setattr(mitigation, "_ROLLOUT_POLL_INTERVAL", 0.01)


def _object(name, deleting=False):
    return SimpleNamespace(metadata=SimpleNamespace(name=name, deletion_timestamp="now" if deleting else None))


def _deployment(name):
    return SimpleNamespace(
        metadata=SimpleNamespace(name=name, generation=1),
        spec=SimpleNamespace(replicas=1),
        status=SimpleNamespace(
            replicas=1,
            observed_generation=1,
            available_replicas=1,
            updated_replicas=1,
            ready_replicas=1,
            unavailable_replicas=0,
        ),
    )


def _pod(name):
    ready = SimpleNamespace(name="c", ready=True, state=SimpleNamespace(waiting=None, terminated=None))
    return SimpleNamespace(
        metadata=SimpleNamespace(name=name), status=SimpleNamespace(phase="Running", container_statuses=[ready])
    )


class _Cluster:
    """Healthy app: two Deployments with Ready pods, plus the objects they use."""

    def __init__(self):
        self.objects = {
            "service": [_object("frontend"), _object("mongodb-reservation")],
            "persistent_volume_claim": [_object("reservation-pvc")],
            "config_map": [_object("mongo-geo-script")],
            "stateful_set": [_object("opensearch")],
        }
        self.core_v1_api = self.apps_v1_api = self

    def __getattr__(self, name):
        kind = name.removeprefix("list_namespaced_")
        return lambda namespace: SimpleNamespace(items=list(self.objects[kind]))

    def list_deployments(self, namespace):
        return SimpleNamespace(items=[_deployment("frontend"), _deployment("reservation")])

    def list_pods(self, namespace):
        return SimpleNamespace(items=[_pod("frontend-1"), _pod("reservation-1")])

    def remove(self, kind, name):
        self.objects[kind] = [o for o in self.objects[kind] if o.metadata.name != name]


@pytest.fixture
def cluster():
    return _Cluster()


@pytest.fixture
def oracle(cluster):
    oracle = MitigationOracle(SimpleNamespace(kubectl=cluster, namespace="hotel-reservation"))
    oracle.capture_baseline()
    return oracle


def test_healthy_app_passes(oracle):
    assert oracle.evaluate() == {"success": True}


@pytest.mark.parametrize(
    ("kind", "name"),
    [
        ("service", "mongodb-reservation"),
        ("persistent_volume_claim", "reservation-pvc"),
        ("config_map", "mongo-geo-script"),
        ("stateful_set", "opensearch"),
    ],
)
def test_deleted_baseline_object_fails(oracle, cluster, kind, name):
    cluster.remove(kind, name)
    verdict = oracle.evaluate()
    assert verdict["success"] is False
    assert verdict["reason"] == "required_resource_missing"
    assert verdict["failure_class"] == "agent_error"


def test_pvc_stuck_terminating_fails(oracle, cluster):
    """`kubectl delete pvc` on a mounted claim leaves it Terminating: still a deletion."""
    cluster.objects["persistent_volume_claim"] = [_object("reservation-pvc", deleting=True)]
    assert oracle.evaluate()["reason"] == "required_resource_missing"


def test_objects_added_after_the_baseline_are_allowed(oracle, cluster):
    cluster.objects["config_map"].append(_object("frontend-env"))
    cluster.objects["service"].append(_object("frontend-canary"))
    assert oracle.evaluate() == {"success": True}
