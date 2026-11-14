from itertools import count
from types import SimpleNamespace

import pytest
from kubernetes.client.rest import ApiException

from sregym.conductor.oracles.capacity_grace_expiry_mitigation import (
    CapacityGraceExpiryMitigationOracle,
    read_policy,
    read_usage,
)
from sregym.conductor.oracles.failure import FailureClass
from sregym.conductor.oracles.mitigation import MitigationOracle

LOGIN_OK = '  HTTP/1.1 200 OK\n{"message":"Login successfully!"}'
LOGIN_500 = "  HTTP/1.1 500 Internal Server Error\nwget: server returned error"
LOGIN_REJECTED = '  HTTP/1.1 200 OK\n{"message":"Failed. Please check your username and password. "}'
NO_ANSWER = "Connecting to frontend:5000 (10.96.1.2:5000)\nwget: download timed out"
INJECTED_START = 1_000
POLICY = {"enforcement": "enabled", "effective_from": str(INJECTED_START), "sync_interval_seconds": "0"}
MISSING = object()


def _deployment(replicas, ready):
    return SimpleNamespace(
        spec=SimpleNamespace(replicas=replicas),
        status=SimpleNamespace(ready_replicas=ready),
    )


def _pod(name, phase="Running"):
    return SimpleNamespace(metadata=SimpleNamespace(name=name), status=SimpleNamespace(phase=phase))


class _Api:
    def __init__(self, objects):
        self.objects = objects

    def _read(self, name, namespace):
        value = self.objects.get(name, MISSING)
        if callable(value):
            value = value()
        if value is MISSING:
            raise ApiException(status=404)
        return value

    read_namespaced_deployment = _read
    read_namespaced_config_map = _read


class _KubeCtl:
    """A cluster in which the controller completes a sync between every two looks, unless ``syncing`` is off."""

    def __init__(
        self,
        *,
        target_states=((1, 1),),
        logins=(LOGIN_OK,),
        controller_replicas=1,
        usage=None,
        policy=None,
        status="syncing",
        pods=None,
    ):
        self.target_states = list(target_states)
        self.logins = list(logins)
        self.commands = []
        self.pods = pods if pods is not None else [_pod("frontend-1"), _pod("consul-1")]
        self.apps_v1_api = _Api(
            {}
            if controller_replicas is None
            else {"capacity-controller": SimpleNamespace(spec=SimpleNamespace(replicas=controller_replicas))}
        )
        ticks = count(1)
        statuses = {
            "syncing": lambda: SimpleNamespace(data={"syncs": str(next(ticks)), "last_sync": "0"}),
            "stalled": lambda: SimpleNamespace(data={"syncs": "7", "last_sync": "0"}),
            "missing": MISSING,
        }
        self.core_v1_api = _Api(
            {
                "capacity-usage": SimpleNamespace(data=usage if usage is not None else {"user": "0"}),
                "capacity-policy": SimpleNamespace(data=policy if policy is not None else dict(POLICY)),
                "capacity-status": statuses[status],
            }
        )

    def get_deployment(self, name, namespace):
        state = self.target_states.pop(0) if len(self.target_states) > 1 else self.target_states[0]
        return _deployment(*state)

    def list_pods(self, namespace):
        return SimpleNamespace(items=self.pods)

    def exec_command_checked(self, command, timeout=None):
        self.commands.append(command)
        return self.logins.pop(0) if len(self.logins) > 1 else self.logins[0]


@pytest.fixture(autouse=True)
def _healthy_generic_checks(monkeypatch):
    monkeypatch.setattr(MitigationOracle, "evaluate", lambda self: {"success": True})


def _oracle(kubectl, *, effective_from=INJECTED_START):
    problem = SimpleNamespace(
        kubectl=kubectl,
        namespace="hotel-reservation",
        faulty_service="user",
        controller_name="capacity-controller",
        controller_namespace="capacity-system",
        usage_configmap="capacity-usage",
        policy_configmap="capacity-policy",
        status_configmap="capacity-status",
        sync_interval_seconds=0,
        _effective_from=effective_from,
    )
    oracle = CapacityGraceExpiryMitigationOracle.__new__(CapacityGraceExpiryMitigationOracle)
    oracle.problem = problem
    oracle.settle_seconds = 0
    oracle.poll_seconds = 0
    oracle.observed_syncs = 1
    return oracle


# -- reading configuration the way the controller does ----------------------------


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (POLICY, (True, 1_000, 0)),
        ({**POLICY, "effective_from": " 5000 "}, (True, 5_000, 0)),
        ({**POLICY, "effective_from": "5000\n"}, (True, 5_000, 0)),
        ({**POLICY, "sync_interval_seconds": "\t3600 "}, (True, 1_000, 3_600)),
        ({**POLICY, "enforcement": " enabled "}, (True, 1_000, 0)),
        ({**POLICY, "enforcement": "Enabled"}, (False, 1_000, 0)),
        ({**POLICY, "enforcement": "disabled"}, (False, 1_000, 0)),
        ({**POLICY, "effective_from": "soon"}, (False, None, 0)),
        ({**POLICY, "effective_from": "١٠٠٠"}, (False, None, 0)),
        ({**POLICY, "sync_interval_seconds": "fast"}, (True, 1_000, None)),
        # A value with a space in it shifts the fields that follow, as it does in the shell.
        ({**POLICY, "effective_from": "5000 77"}, (True, 5_000, 77)),
        ({"enforcement": "enabled", "sync_interval_seconds": "10"}, (False, None, None)),
        ({}, (False, None, None)),
        (None, (False, None, None)),
    ],
)
def test_policy_is_read_the_way_the_controller_reads_it(data, expected):
    assert read_policy(data) == expected


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        ({"user": "0"}, 0),
        ({"user": " 0 "}, 0),
        ({"user": "0\n"}, 0),
        ({"user": "2 extra"}, 2),
        ({"user": ""}, None),
        ({"user": "none"}, None),
        ({"user": "-1"}, None),
        ({"user": "٠"}, None),
        ({"search": "1"}, None),
        ({}, None),
        (None, None),
    ],
)
def test_usage_record_is_read_the_way_the_controller_reads_it(data, expected):
    assert read_usage(data, "user") == expected


# -- verdicts ---------------------------------------------------------------------


def test_generic_failure_is_returned_unchanged(monkeypatch):
    verdict = MitigationOracle.fail("required_deployment_scaled_to_zero", deployment="user")
    monkeypatch.setattr(MitigationOracle, "evaluate", lambda self: verdict)

    assert _oracle(_KubeCtl()).evaluate() is verdict


def test_passes_when_usage_record_is_corrected_and_the_controller_keeps_syncing():
    kubectl = _KubeCtl(usage={"user": "1"})

    assert _oracle(kubectl).evaluate() == {"success": True}
    assert "consul-1" in kubectl.commands[0]
    assert "/user?username=" in kubectl.commands[0]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"controller_replicas": 0},
        {"controller_replicas": None},
        {"policy": {**POLICY, "enforcement": "disabled"}},
    ],
    ids=["controller-stopped", "controller-removed", "enforcement-disabled"],
)
def test_stopping_enforcement_passes_without_waiting_for_syncs(kwargs):
    kubectl = _KubeCtl(status="stalled", **kwargs)

    assert _oracle(kubectl).evaluate() == {"success": True}


def test_fails_while_target_has_no_ready_replica():
    result = _oracle(_KubeCtl(target_states=[(1, 0)])).evaluate()

    assert result["success"] is False
    assert result["reason"] == "fault_still_present"


def test_fails_when_restored_replicas_are_removed_again():
    result = _oracle(_KubeCtl(target_states=[(1, 1), (0, 0)])).evaluate()

    assert result["reason"] == "fix_reverted_by_automation"
    assert result["failure_class"] == FailureClass.AGENT_ERROR


@pytest.mark.parametrize(
    ("policy", "change"),
    [
        ({**POLICY, "effective_from": "5000"}, "effective_from"),
        ({**POLICY, "effective_from": "5000 "}, "effective_from"),
        ({**POLICY, "effective_from": "\n5000"}, "effective_from"),
        ({**POLICY, "sync_interval_seconds": "3600"}, "sync_interval"),
        ({**POLICY, "sync_interval_seconds": " 3600\n"}, "sync_interval"),
    ],
    ids=["later-start", "later-start-padded", "later-start-leading-newline", "slower-sync", "slower-sync-padded"],
)
def test_fails_when_enforcement_is_only_postponed(policy, change):
    kubectl = _KubeCtl(policy=policy)

    result = _oracle(kubectl).evaluate()

    assert result["reason"] == "enforcement_only_deferred"
    assert change in result["detail"]["change"]
    assert kubectl.commands == []


@pytest.mark.parametrize("usage", [{"user": "0"}, {"user": " 0"}, {"user": "0\n"}])
def test_postponement_is_caught_however_the_wrong_record_is_padded(usage):
    kubectl = _KubeCtl(policy={**POLICY, "effective_from": "5000"}, usage=usage)

    assert _oracle(kubectl).evaluate()["reason"] == "enforcement_only_deferred"


def test_postponement_is_fine_once_the_usage_record_is_right():
    policy = {**POLICY, "effective_from": "5000"}
    kubectl = _KubeCtl(policy=policy, usage={"user": "1"})

    assert _oracle(kubectl).evaluate() == {"success": True}


def test_fails_when_an_enabled_controller_is_never_seen_reconciling():
    kubectl = _KubeCtl(usage={"user": "1"}, status="stalled")

    result = _oracle(kubectl).evaluate()

    assert result["reason"] == "reconciliation_not_observed"
    assert result["failure_class"] == FailureClass.AMBIGUOUS
    assert result["detail"]["syncs_seen"] == 0


def test_every_required_sync_has_to_be_seen():
    oracle = _oracle(_KubeCtl(usage={"user": "1"}))
    oracle.observed_syncs = 3

    assert oracle.evaluate()["detail"] == {
        "controller": "capacity-controller",
        "syncs_seen": 1,
        "syncs_required": 3,
    }


def test_falls_back_to_the_clock_when_the_progress_record_is_gone():
    kubectl = _KubeCtl(usage={"user": "1"}, status="missing")

    assert _oracle(kubectl).evaluate() == {"success": True}


@pytest.mark.parametrize(
    "response", [LOGIN_500, LOGIN_REJECTED, NO_ANSWER], ids=["http-500", "200-without-login", "timeout"]
)
def test_fails_when_logins_do_not_work(response):
    kubectl = _KubeCtl(logins=[response], controller_replicas=0)

    result = _oracle(kubectl).evaluate()

    assert result["reason"] == "login_failing"
    assert result["failure_class"] == FailureClass.AGENT_ERROR


def test_unanswered_probes_are_retried_while_logins_settle():
    kubectl = _KubeCtl(logins=[NO_ANSWER, LOGIN_500, LOGIN_OK], controller_replicas=0)
    oracle = _oracle(kubectl)
    oracle.settle_seconds = 30

    assert oracle.evaluate() == {"success": True}
    assert len(kubectl.commands) == 4


def test_a_failed_login_at_the_end_of_the_window_must_be_followed_by_a_good_one():
    kubectl = _KubeCtl(logins=[LOGIN_OK, LOGIN_500, LOGIN_OK], controller_replicas=0)
    assert _oracle(kubectl).evaluate() == {"success": True}
    assert len(kubectl.commands) == 3

    kubectl = _KubeCtl(logins=[LOGIN_OK, LOGIN_500, LOGIN_500], controller_replicas=0)
    assert _oracle(kubectl).evaluate()["reason"] == "login_failing"


def test_missing_probe_pod_is_a_harness_error():
    kubectl = _KubeCtl(pods=[_pod("frontend-1")], controller_replicas=0)

    result = _oracle(kubectl).evaluate()

    assert result["reason"] == "no_probe_pod_available"
    assert result["failure_class"] == FailureClass.HARNESS_ERROR


@pytest.mark.parametrize("output", ["sh: wget: not found", "sh: 1: wget: not found", "wget: No such file or directory"])
def test_missing_probe_tooling_is_reported_at_once_not_retried(output):
    kubectl = _KubeCtl(logins=[output, LOGIN_OK], controller_replicas=0)
    oracle = _oracle(kubectl)
    oracle.settle_seconds = 30

    result = oracle.evaluate()

    assert result["reason"] == "http_probe_response_missing"
    assert result["failure_class"] == FailureClass.AMBIGUOUS
    assert len(kubectl.commands) == 1


def test_failure_to_exec_into_the_probe_pod_is_classified_not_retried():
    kubectl = _KubeCtl(controller_replicas=0)
    calls = []

    def boom(command, timeout=None):
        calls.append(command)
        raise RuntimeError("Command failed (exit 1): kubectl exec")

    kubectl.exec_command_checked = boom
    oracle = _oracle(kubectl)
    oracle.settle_seconds = 30

    assert oracle.evaluate()["success"] is False
    assert len(calls) == 1
