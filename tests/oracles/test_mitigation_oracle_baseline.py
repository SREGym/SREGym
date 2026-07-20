"""Baseline-capture behaviour of the generic MitigationOracle.

The oracle compares the post-submission cluster against a pre-fault baseline to
reject reward hacks (deleting a deployment, scaling it to 0). That baseline must
be captured while the app is actually deployed. The Problem — and therefore the
oracle — is constructed before `deploy_app()`, so capture cannot happen in
`__init__`; it is driven by `capture_baseline()` from `Conductor._inject_fault`.
"""

from types import SimpleNamespace

import pytest

from sregym.conductor.conductor import Conductor
from sregym.conductor.oracles import mitigation
from sregym.conductor.oracles.base import Oracle
from sregym.conductor.oracles.compound import CompoundedOracle
from sregym.conductor.oracles.mitigation import MitigationOracle


@pytest.fixture(autouse=True)
def _fast_rollout_settle(monkeypatch):
    """These tests exercise baseline logic, not rollout settling. Without this
    an unsettled deployment burns the full 60s settle window."""
    monkeypatch.setattr(mitigation, "_ROLLOUT_SETTLE_SECONDS", 0.05)
    monkeypatch.setattr(mitigation, "_ROLLOUT_POLL_INTERVAL", 0.01)


def _deployment(name, *, replicas=1, ready=None, updated=None, unavailable=0):
    ready = replicas if ready is None else ready
    updated = replicas if updated is None else updated
    return SimpleNamespace(
        metadata=SimpleNamespace(name=name),
        spec=SimpleNamespace(replicas=replicas),
        status=SimpleNamespace(
            updated_replicas=updated,
            ready_replicas=ready,
            unavailable_replicas=unavailable,
        ),
    )


def _pod(name, *, phase="Running", ready=True):
    return SimpleNamespace(
        metadata=SimpleNamespace(name=name),
        status=SimpleNamespace(
            phase=phase,
            container_statuses=[
                SimpleNamespace(
                    name="c",
                    ready=ready,
                    state=SimpleNamespace(waiting=None, terminated=None),
                )
            ],
        ),
    )


class _KubeCtl:
    """Mutable fake cluster: set_state() models deploy / hack transitions."""

    def __init__(self, deployments=(), pods=()):
        self.set_state(deployments, pods)

    def set_state(self, deployments=(), pods=()):
        self._deployments = list(deployments)
        self._pods = list(pods)

    def list_deployments(self, namespace):
        return SimpleNamespace(items=list(self._deployments))

    def list_pods(self, namespace):
        return SimpleNamespace(items=list(self._pods))


class _Problem:
    def __init__(self, kubectl, namespace="demo-ns"):
        self.kubectl = kubectl
        self.namespace = namespace


@pytest.fixture
def kubectl():
    # Namespace does not exist yet: this is the state at conductor.py's
    # get_problem_instance(), which runs before undeploy_app()/deploy_app().
    return _KubeCtl()


@pytest.fixture
def oracle(kubectl):
    return MitigationOracle(_Problem(kubectl))


def _deploy_two(kubectl):
    kubectl.set_state(
        [_deployment("web-a"), _deployment("web-b")],
        [_pod("web-a-1"), _pod("web-b-1")],
    )


def test_construction_against_absent_namespace_does_not_raise(kubectl):
    """The oracle is built before the namespace exists; that must not explode."""
    assert MitigationOracle(_Problem(kubectl)).replica_count == {}


def test_capture_baseline_records_replica_counts(oracle, kubectl):
    _deploy_two(kubectl)
    oracle.capture_baseline()
    assert oracle.replica_count == {"web-a": 1, "web-b": 1}


def test_capture_baseline_overwrites_a_stale_snapshot(oracle, kubectl):
    """Leftovers from a previous run must not leak into this run's baseline."""
    kubectl.set_state([_deployment("stale-dep", replicas=3)], [])
    oracle.capture_baseline()
    _deploy_two(kubectl)
    oracle.capture_baseline()
    assert oracle.replica_count == {"web-a": 1, "web-b": 1}


def test_healthy_cluster_passes(oracle, kubectl):
    _deploy_two(kubectl)
    oracle.capture_baseline()
    assert oracle.evaluate()["success"] is True


def test_scaling_a_deployment_to_zero_is_rejected(oracle, kubectl):
    """Reward hack: zero out one deployment. Other pods stay Running, so the
    pod-liveness check alone would pass — only the baseline catches this."""
    _deploy_two(kubectl)
    oracle.capture_baseline()

    kubectl.set_state(
        [_deployment("web-a"), _deployment("web-b", replicas=0, ready=0, updated=0)],
        [_pod("web-a-1")],
    )

    assert oracle.evaluate()["success"] is False


def test_deleting_a_deployment_is_rejected(oracle, kubectl):
    _deploy_two(kubectl)
    oracle.capture_baseline()

    kubectl.set_state([_deployment("web-a")], [_pod("web-a-1")])

    assert oracle.evaluate()["success"] is False


def test_under_replicated_deployment_is_rejected(oracle, kubectl):
    _deploy_two(kubectl)
    oracle.capture_baseline()

    kubectl.set_state(
        [_deployment("web-a"), _deployment("web-b", replicas=2, ready=1, updated=2)],
        [_pod("web-a-1"), _pod("web-b-1")],
    )

    assert oracle.evaluate()["success"] is False


def test_without_baseline_capture_the_hack_would_slip_through(kubectl):
    """Regression guard for the original defect.

    If the baseline is never captured, replica_count stays empty and every
    replica check is skipped — a zeroed deployment then passes on the strength
    of the surviving pods alone. This test pins that failure mode so the
    capture_baseline() call site cannot be quietly dropped again.
    """
    oracle = MitigationOracle(_Problem(kubectl))
    kubectl.set_state(
        [_deployment("web-a"), _deployment("web-b", replicas=0, ready=0, updated=0)],
        [_pod("web-a-1")],
    )

    assert oracle.replica_count == {}
    assert oracle.evaluate()["success"] is True  # the bug, if capture is skipped


def test_base_oracle_capture_baseline_is_a_noop():
    """Oracles that need no baseline inherit a harmless default."""

    class _Custom(Oracle):
        def evaluate(self, solution=None, trace=None, duration=None):
            return {"success": True}

    _Custom(problem=object()).capture_baseline()


def test_conductor_captures_baseline_before_injecting_the_fault():
    """Ordering is the whole point: a baseline taken after injection would
    record the broken state as healthy."""
    calls = []
    problem = SimpleNamespace(
        mitigation_oracle=SimpleNamespace(capture_baseline=lambda: calls.append("baseline")),
        inject_fault=lambda: calls.append("inject"),
        diagnosis_oracle=None,
    )
    conductor = SimpleNamespace(
        current_problem=problem,
        logger=SimpleNamespace(info=lambda *a, **k: None),
        fault_injected=False,
    )

    Conductor._inject_fault(conductor)

    assert calls == ["baseline", "inject"]
    assert conductor.fault_injected is True


def test_conductor_captures_baseline_through_nested_compounded_oracles(kubectl):
    problem = _Problem(kubectl)
    child_oracles = [MitigationOracle(problem), MitigationOracle(problem)]
    inner_oracle = CompoundedOracle(problem, *child_oracles)
    problem.mitigation_oracle = CompoundedOracle(problem, inner_oracle)
    problem.inject_fault = lambda: None
    problem.diagnosis_oracle = None
    conductor = SimpleNamespace(
        current_problem=problem,
        logger=SimpleNamespace(info=lambda *a, **k: None),
        fault_injected=False,
    )
    _deploy_two(kubectl)

    Conductor._inject_fault(conductor)

    assert all(oracle.replica_count == {"web-a": 1, "web-b": 1} for oracle in child_oracles)
    kubectl.set_state(
        [_deployment("web-a"), _deployment("web-b", replicas=0, ready=0, updated=0)],
        [_pod("web-a-1")],
    )
    assert problem.mitigation_oracle.evaluate()["success"] is False


def test_conductor_tolerates_a_problem_without_a_mitigation_oracle():
    calls = []
    problem = SimpleNamespace(
        mitigation_oracle=None,
        inject_fault=lambda: calls.append("inject"),
        diagnosis_oracle=None,
    )
    conductor = SimpleNamespace(
        current_problem=problem,
        logger=SimpleNamespace(info=lambda *a, **k: None),
        fault_injected=False,
    )

    Conductor._inject_fault(conductor)

    assert calls == ["inject"]
