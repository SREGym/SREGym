import re
from types import SimpleNamespace

import pytest

from sregym.conductor.problems import capacity_grace_expiry
from sregym.conductor.problems.capacity_grace_expiry import CapacityGraceExpiry


def _deployment(name, replicas):
    return SimpleNamespace(metadata=SimpleNamespace(name=name), spec=SimpleNamespace(replicas=replicas))


def _pod(service, *, terminating=False):
    return SimpleNamespace(
        metadata=SimpleNamespace(
            labels={"io.kompose.service": service},
            deletion_timestamp="2026-01-01T00:00:00Z" if terminating else None,
        )
    )


class _KubeCtl:
    def __init__(self, deployments, pods=()):
        self.deployments = {d.metadata.name: d for d in deployments}
        self.pods = list(pods)
        self.scales = []

    def get_deployment(self, name, namespace):
        return self.deployments[name]

    def list_deployments(self, namespace):
        return SimpleNamespace(items=list(self.deployments.values()))

    def list_pods(self, namespace):
        return SimpleNamespace(items=self.pods)

    def scale_deployment(self, name, namespace, replicas):
        self.scales.append((name, namespace, replicas))
        self.deployments[name].spec.replicas = replicas


def _problem(kubectl):
    problem = CapacityGraceExpiry.__new__(CapacityGraceExpiry)
    problem.kubectl = kubectl
    problem.namespace = "hotel-reservation"
    problem.faulty_service = "user"
    problem._original_replicas = None
    problem._effective_from = None
    problem.poll_interval_seconds = 0
    return problem


def test_usage_record_matches_every_deployment_except_the_target():
    kubectl = _KubeCtl([_deployment("frontend", 1), _deployment("search", 3), _deployment("user", 1)])

    assert _problem(kubectl)._usage_data() == {"frontend": "1", "search": "3", "user": "0"}


def test_enforcement_starts_one_grace_period_after_cluster_creation_time(monkeypatch):
    problem = _problem(_KubeCtl([_deployment("user", 2)]))
    calls = []
    monkeypatch.setattr(problem, "_teardown_controller", lambda: calls.append("teardown"))
    monkeypatch.setattr(problem, "_create_namespace", lambda: 1_000)
    for step in ("_create_rbac", "_create_configmaps", "_create_controller"):
        monkeypatch.setattr(problem, step, lambda step=step: calls.append(step))
    monkeypatch.setattr(problem, "_wait_for_target_down", lambda timeout: calls.append(("wait", timeout)))

    problem.inject_fault()

    assert problem._original_replicas == 2
    assert problem._effective_from == 1_000 + CapacityGraceExpiry.grace_seconds
    assert calls[0] == "teardown"
    assert calls[-1] == ("wait", CapacityGraceExpiry.grace_seconds + CapacityGraceExpiry.inject_timeout_seconds)


def test_injection_waits_until_the_target_has_no_live_pods():
    kubectl = _KubeCtl([_deployment("user", 0)], pods=[_pod("user", terminating=True), _pod("frontend")])

    _problem(kubectl)._wait_for_target_down(timeout=0)


@pytest.mark.parametrize(
    ("replicas", "pods"),
    [(1, [_pod("user")]), (0, [_pod("user")])],
    ids=["not-scaled-down", "pod-still-live"],
)
def test_injection_fails_loudly_if_the_target_stays_up(replicas, pods):
    kubectl = _KubeCtl([_deployment("user", replicas)], pods=pods)

    with pytest.raises(RuntimeError, match="not scaled down in time"):
        _problem(kubectl)._wait_for_target_down(timeout=0)


def test_recovery_removes_the_controller_before_restoring_replicas(monkeypatch):
    kubectl = _KubeCtl([_deployment("user", 0)])
    problem = _problem(kubectl)
    problem._original_replicas = 2
    order = []
    monkeypatch.setattr(problem, "_teardown_controller", lambda: order.append("teardown"))
    monkeypatch.setattr(
        kubectl, "scale_deployment", lambda name, namespace, replicas: order.append((name, namespace, replicas))
    )

    problem.recover_fault()

    assert order == ["teardown", ("user", "hotel-reservation", 2)]


def test_recovery_without_a_recorded_baseline_restores_one_replica_and_never_scales_down():
    kubectl = _KubeCtl([_deployment("user", 0)])
    _problem(kubectl)._restore_replicas()
    assert kubectl.scales == [("user", "hotel-reservation", 1)]

    kubectl = _KubeCtl([_deployment("user", 3)])
    _problem(kubectl)._restore_replicas()
    assert kubectl.scales == []


def test_nothing_the_agent_can_read_describes_the_scenario():
    visible = " ".join(
        [
            capacity_grace_expiry._CONTROLLER_SCRIPT,
            CapacityGraceExpiry.controller_name,
            CapacityGraceExpiry.controller_namespace,
            CapacityGraceExpiry.usage_configmap,
            CapacityGraceExpiry.policy_configmap,
        ]
    )

    assert not re.search(r"fault|inject|chaos|sregym|bug|outage|incident|wrong|#", visible, re.IGNORECASE)
    assert "echo" not in capacity_grace_expiry._CONTROLLER_SCRIPT
