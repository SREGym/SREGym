from types import SimpleNamespace

import pytest
from kubernetes.client.rest import ApiException

from sregym.conductor.oracles.capacity_grace_expiry_mitigation import CapacityGraceExpiryMitigationOracle
from sregym.conductor.oracles.failure import FailureClass
from sregym.conductor.oracles.mitigation import MitigationOracle

LOGIN_OK = '  HTTP/1.1 200 OK\n{"message":"Login successfully!"}'
LOGIN_500 = "  HTTP/1.1 500 Internal Server Error\nwget: server returned error"
LOGIN_REJECTED = '  HTTP/1.1 200 OK\n{"message":"Failed. Please check your username and password. "}'
INJECTED_START = 1_000


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
        if name not in self.objects:
            raise ApiException(status=404)
        return self.objects[name]

    read_namespaced_deployment = _read
    read_namespaced_config_map = _read


class _KubeCtl:
    def __init__(self, *, target_states, logins, controller_replicas=1, usage=None, policy=None, pods=None):
        self.target_states = list(target_states)
        self.logins = list(logins)
        self.commands = []
        self.pods = pods if pods is not None else [_pod("frontend-1"), _pod("consul-1")]
        self.apps_v1_api = _Api(
            {}
            if controller_replicas is None
            else {"capacity-controller": SimpleNamespace(spec=SimpleNamespace(replicas=controller_replicas))}
        )
        self.core_v1_api = _Api(
            {
                "capacity-usage": SimpleNamespace(data=usage if usage is not None else {"user": "0"}),
                "capacity-policy": SimpleNamespace(
                    data=policy
                    if policy is not None
                    else {"enforcement": "enabled", "effective_from": str(INJECTED_START), "sync_interval_seconds": "0"}
                ),
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
        sync_interval_seconds=0,
        _effective_from=effective_from,
    )
    oracle = CapacityGraceExpiryMitigationOracle.__new__(CapacityGraceExpiryMitigationOracle)
    oracle.problem = problem
    oracle.settle_seconds = 0
    oracle.poll_seconds = 0
    return oracle


def test_generic_failure_is_returned_unchanged(monkeypatch):
    verdict = MitigationOracle.fail("required_deployment_scaled_to_zero", deployment="user")
    monkeypatch.setattr(MitigationOracle, "evaluate", lambda self: verdict)

    assert _oracle(_KubeCtl(target_states=[(1, 1)], logins=[LOGIN_OK])).evaluate() is verdict


def test_passes_when_usage_record_is_corrected():
    kubectl = _KubeCtl(target_states=[(1, 1)], logins=[LOGIN_OK], usage={"user": "1"})

    assert _oracle(kubectl).evaluate() == {"success": True}
    assert "consul-1" in kubectl.commands[0]
    assert "/user?username=" in kubectl.commands[0]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"controller_replicas": 0},
        {"controller_replicas": None},
        {"policy": {"enforcement": "disabled", "effective_from": "9999", "sync_interval_seconds": "0"}},
    ],
    ids=["controller-stopped", "controller-removed", "enforcement-disabled"],
)
def test_passes_for_each_way_of_stopping_enforcement(kwargs):
    kubectl = _KubeCtl(target_states=[(1, 1)], logins=[LOGIN_OK], **kwargs)

    assert _oracle(kubectl).evaluate() == {"success": True}


def test_fails_while_target_has_no_ready_replica():
    result = _oracle(_KubeCtl(target_states=[(1, 0)], logins=[LOGIN_OK])).evaluate()

    assert result["success"] is False
    assert result["reason"] == "fault_still_present"


def test_fails_when_restored_replicas_are_removed_again():
    kubectl = _KubeCtl(target_states=[(1, 1), (0, 0)], logins=[LOGIN_OK])

    result = _oracle(kubectl).evaluate()

    assert result["reason"] == "fix_reverted_by_automation"
    assert result["failure_class"] == FailureClass.AGENT_ERROR


@pytest.mark.parametrize(
    ("policy", "change"),
    [
        ({"enforcement": "enabled", "effective_from": "5000", "sync_interval_seconds": "0"}, "effective_from"),
        ({"enforcement": "enabled", "effective_from": "1000", "sync_interval_seconds": "3600"}, "sync_interval"),
    ],
)
def test_fails_when_enforcement_is_only_postponed(policy, change):
    kubectl = _KubeCtl(target_states=[(1, 1)], logins=[LOGIN_OK], policy=policy)

    result = _oracle(kubectl).evaluate()

    assert result["reason"] == "enforcement_only_deferred"
    assert change in result["detail"]["change"]
    assert kubectl.commands == []


def test_postponement_is_fine_once_the_usage_record_is_right():
    policy = {"enforcement": "enabled", "effective_from": "5000", "sync_interval_seconds": "3600"}
    kubectl = _KubeCtl(target_states=[(1, 1)], logins=[LOGIN_OK], policy=policy, usage={"user": "1"})

    assert _oracle(kubectl).evaluate() == {"success": True}


@pytest.mark.parametrize("response", [LOGIN_500, LOGIN_REJECTED], ids=["http-500", "200-without-login"])
def test_fails_when_logins_do_not_work(response):
    kubectl = _KubeCtl(target_states=[(1, 1)], logins=[response], controller_replicas=0)

    result = _oracle(kubectl).evaluate()

    assert result["reason"] == "login_failing"
    assert result["failure_class"] == FailureClass.AGENT_ERROR


def test_one_failed_login_in_the_window_is_tolerated_but_two_are_not():
    oracle = _oracle(_KubeCtl(target_states=[(1, 1)], logins=[LOGIN_OK, LOGIN_500], controller_replicas=0))
    assert oracle.evaluate() == {"success": True}

    oracle = _oracle(_KubeCtl(target_states=[(1, 1)], logins=[LOGIN_OK, LOGIN_500], controller_replicas=0))
    oracle.problem.sync_interval_seconds = 0.05
    oracle.observed_syncs = 1
    assert oracle.evaluate()["reason"] == "login_failing"


def test_missing_probe_pod_is_a_harness_error():
    kubectl = _KubeCtl(target_states=[(1, 1)], logins=[LOGIN_OK], pods=[_pod("frontend-1")], controller_replicas=0)

    result = _oracle(kubectl).evaluate()

    assert result["reason"] == "no_probe_pod_available"
    assert result["failure_class"] == FailureClass.HARNESS_ERROR


def test_unparseable_probe_output_is_ambiguous():
    kubectl = _KubeCtl(target_states=[(1, 1)], logins=["sh: wget: not found"], controller_replicas=0)

    result = _oracle(kubectl).evaluate()

    assert result["reason"] == "http_probe_response_missing"
    assert result["failure_class"] == FailureClass.AMBIGUOUS


def test_probe_command_failure_is_classified_not_raised():
    kubectl = _KubeCtl(target_states=[(1, 1)], logins=[LOGIN_OK], controller_replicas=0)

    def boom(command, timeout=None):
        raise RuntimeError("Command failed (exit 1): kubectl exec")

    kubectl.exec_command_checked = boom

    assert _oracle(kubectl).evaluate()["success"] is False
