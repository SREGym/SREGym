import re
import subprocess
from types import SimpleNamespace

import pytest
from kubernetes.client.rest import ApiException

from sregym.conductor.problems import capacity_grace_expiry
from sregym.conductor.problems.capacity_grace_expiry import CapacityGraceExpiry


def _deployment(name, replicas, ready=None):
    return SimpleNamespace(
        metadata=SimpleNamespace(name=name),
        spec=SimpleNamespace(replicas=replicas),
        status=SimpleNamespace(ready_replicas=replicas if ready is None else ready),
    )


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
            CapacityGraceExpiry.status_configmap,
        ]
    )

    assert not re.search(r"fault|inject|chaos|sregym|bug|outage|incident|wrong|#", visible, re.IGNORECASE)
    assert "echo" not in capacity_grace_expiry._CONTROLLER_SCRIPT


def test_controller_permissions_are_limited_to_what_it_needs():
    problem = _problem(_KubeCtl([]))

    assert problem._own_namespace_rules() == [
        {
            "apiGroups": [""],
            "resources": ["configmaps"],
            "resourceNames": ["capacity-usage", "capacity-policy", "capacity-status"],
            "verbs": ["get"],
        },
        {"apiGroups": [""], "resources": ["configmaps"], "resourceNames": ["capacity-status"], "verbs": ["patch"]},
    ]
    app_rules = CapacityGraceExpiry._app_namespace_rules()
    assert {(rule["resources"][0], verb) for rule in app_rules for verb in rule["verbs"]} == {
        ("deployments", "get"),
        ("deployments", "list"),
        ("deployments/scale", "get"),
        ("deployments/scale", "patch"),
        ("deployments/scale", "update"),
    }
    assert all(rule["apiGroups"] == ["apps"] for rule in app_rules)


def test_controller_manifest_is_pinned_and_wired_to_its_configmaps():
    problem = _problem(_KubeCtl([]))

    manifest = problem._controller_manifest()
    pod = manifest["spec"]["template"]["spec"]
    container = pod["containers"][0]

    assert "@sha256:" in container["image"]
    assert pod["serviceAccountName"] == "capacity-controller"
    assert manifest["spec"]["selector"]["matchLabels"] == manifest["spec"]["template"]["metadata"]["labels"]
    assert {env["name"]: env["value"] for env in container["env"]} == {
        "TARGET_NAMESPACE": "hotel-reservation",
        "USAGE_CONFIGMAP": "capacity-usage",
        "POLICY_CONFIGMAP": "capacity-policy",
        "STATUS_CONFIGMAP": "capacity-status",
    }


def test_policy_carries_the_start_time_and_sync_interval():
    problem = _problem(_KubeCtl([]))
    problem._effective_from = 1_060

    assert problem._policy_data() == {
        "enforcement": "enabled",
        "effective_from": "1060",
        "sync_interval_seconds": str(CapacityGraceExpiry.sync_interval_seconds),
    }


class _Recorder:
    """Records delete calls; ``read_namespace`` reports the namespace gone after ``reads_until_gone`` reads."""

    def __init__(self, calls, *, missing=(), reads_until_gone=0):
        self.calls = calls
        self.missing = set(missing)
        self.reads_left = reads_until_gone

    def __getattr__(self, method):
        def call(*args, **kwargs):
            self.calls.append((method, *args))
            if method in self.missing:
                raise ApiException(status=404)

        return call

    def read_namespace(self, name):
        if self.reads_left <= 0:
            raise ApiException(status=404)
        self.reads_left -= 1


def test_removal_stops_the_controller_first_and_waits_for_the_namespace():
    calls = []
    core = _Recorder(calls, reads_until_gone=2)

    CapacityGraceExpiry._remove_controller(core, _Recorder(calls), _Recorder(calls), "hotel-reservation", poll=0)

    assert calls == [
        ("delete_namespaced_deployment", "capacity-controller", "capacity-system"),
        ("delete_namespaced_role_binding", "capacity-controller", "hotel-reservation"),
        ("delete_namespaced_role", "capacity-controller", "hotel-reservation"),
        ("delete_namespace", "capacity-system"),
    ]
    assert core.reads_left == 0


def test_removal_is_a_no_op_when_nothing_is_left():
    calls = []
    gone = {"delete_namespaced_deployment", "delete_namespaced_role_binding", "delete_namespaced_role"}

    CapacityGraceExpiry._remove_controller(
        _Recorder(calls, missing={"delete_namespace"}),
        _Recorder(calls, missing=gone),
        _Recorder(calls, missing=gone),
        "hotel-reservation",
        poll=0,
    )

    assert len(calls) == 4


def test_removal_reports_a_namespace_that_will_not_go_away():
    core = _Recorder([], reads_until_gone=10**6)

    with pytest.raises(RuntimeError, match="was not deleted"):
        CapacityGraceExpiry._remove_controller(
            core, _Recorder([]), _Recorder([]), "hotel-reservation", timeout=0, poll=0
        )


_FAKE_KUBECTL = """#!/bin/sh
case "$*" in
  *"get configmap capacity-policy"*) cat "$FAKE_DIR/policy" ;;
  *"get configmap capacity-usage"*) cat "$FAKE_DIR/usage" ;;
  *"get deployments"*) cat "$FAKE_DIR/current" ;;
  *"scale deployment"*) echo "$*" >> "$FAKE_DIR/scaled" ;;
  *"patch configmap capacity-status"*) echo "$*" >> "$FAKE_DIR/progress" ;;
  *) exit 1 ;;
esac
"""

# Ends the controller's loop after one pass and records how long it meant to wait.
_FAKE_SLEEP = """#!/bin/sh
echo "$1" > "$FAKE_DIR/slept"
kill "$PPID"
"""


def _run_one_sync(tmp_path, *, policy, usage, current):
    """Run the controller script for one pass against stand-in commands; no cluster is contacted."""
    for name, body in (("kubectl", _FAKE_KUBECTL), ("sleep", _FAKE_SLEEP)):
        path = tmp_path / name
        path.write_text(body)
        path.chmod(0o755)
    (tmp_path / "policy").write_text(policy)
    (tmp_path / "usage").write_text(usage)
    (tmp_path / "current").write_text(current)
    script = tmp_path / "controller.sh"
    script.write_text(capacity_grace_expiry._CONTROLLER_SCRIPT)
    subprocess.run(
        ["/bin/sh", str(script)],
        env={
            "PATH": f"{tmp_path}:/usr/bin:/bin",
            "FAKE_DIR": str(tmp_path),
            "TARGET_NAMESPACE": "hotel-reservation",
            "USAGE_CONFIGMAP": "capacity-usage",
            "POLICY_CONFIGMAP": "capacity-policy",
            "STATUS_CONFIGMAP": "capacity-status",
        },
        timeout=20,
        check=False,
    )
    scaled = tmp_path / "scaled"
    return (
        scaled.read_text().splitlines() if scaled.exists() else [],
        (tmp_path / "slept").read_text().strip(),
    )


USAGE = "frontend 1\nsearch 2\nuser 0\n"
CURRENT = "frontend 1\nsearch 1\nuser 1\nunlisted 3\n"


def test_controller_script_only_scales_down_deployments_above_their_record(tmp_path):
    scaled, slept = _run_one_sync(tmp_path, policy="enabled 0 10", usage=USAGE, current=CURRENT)

    assert scaled == ["-n hotel-reservation scale deployment user --replicas=0"]
    assert slept == "10"


@pytest.mark.parametrize(
    "policy",
    ["disabled 0 10", "enabled 99999999999 10", "enabled <no value> 10", ""],
    ids=["switched-off", "before-start-time", "start-time-missing", "policy-unreadable"],
)
def test_controller_script_does_nothing_unless_enforcement_is_due(tmp_path, policy):
    scaled, _ = _run_one_sync(tmp_path, policy=policy, usage=USAGE, current=CURRENT)

    assert scaled == []


def test_controller_script_ignores_records_that_are_not_whole_numbers(tmp_path):
    scaled, _ = _run_one_sync(
        tmp_path, policy="enabled 0 10", usage="user none\nsearch -1\nfrontend\n", current=CURRENT
    )

    assert scaled == []


def test_controller_script_falls_back_to_a_sane_sync_interval(tmp_path):
    _, slept = _run_one_sync(tmp_path, policy="enabled 0 soon", usage=USAGE, current=CURRENT)
    assert slept == "10"

    _, slept = _run_one_sync(tmp_path, policy="enabled 0 0", usage=USAGE, current=CURRENT)
    assert slept == "1"


@pytest.mark.parametrize(
    "policy", ["enabled 0 10", "disabled 0 10", ""], ids=["enforcing", "switched-off", "unreadable"]
)
def test_controller_script_records_progress_on_every_pass(tmp_path, policy):
    _run_one_sync(tmp_path, policy=policy, usage=USAGE, current=CURRENT)

    (recorded,) = (tmp_path / "progress").read_text().splitlines()
    assert re.search(r'\{"data":\{"syncs":"1","last_sync":"[0-9]+"\}\}$', recorded)


@pytest.mark.parametrize(
    ("replicas", "ready"),
    [(0, 0), (1, 0)],
    ids=["already-scaled-down", "not-ready"],
)
def test_injection_refuses_to_start_from_an_unhealthy_target(monkeypatch, replicas, ready):
    problem = _problem(_KubeCtl([_deployment("user", replicas, ready)]))
    problem.baseline_timeout_seconds = 0
    monkeypatch.setattr(problem, "_teardown_controller", lambda: pytest.fail("must not touch the cluster"))

    with pytest.raises(RuntimeError, match="not healthy before injection"):
        problem.inject_fault()


def test_injection_waits_for_a_target_that_is_still_starting():
    kubectl = _KubeCtl([])
    looks = iter([_deployment("user", 1, 0), _deployment("user", 1, 0), _deployment("user", 1, 1)])
    kubectl.get_deployment = lambda name, namespace: next(looks)

    assert _problem(kubectl)._healthy_target().status.ready_replicas == 1


def test_a_scaled_down_target_is_rejected_without_waiting():
    problem = _problem(_KubeCtl([_deployment("user", 0, 0)]))
    problem.poll_interval_seconds = 3600

    with pytest.raises(RuntimeError, match="not healthy before injection"):
        problem._healthy_target()
