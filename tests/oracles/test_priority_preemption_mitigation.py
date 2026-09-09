from types import SimpleNamespace

import pytest

from sregym.conductor.oracles.failure import FailureClass
from sregym.conductor.oracles.priority_preemption_mitigation import PriorityPreemptionMitigationOracle


def _oracle():
    oracle = object.__new__(PriorityPreemptionMitigationOracle)
    oracle.problem = SimpleNamespace(
        namespace="hotel-reservation",
        faulty_service="reservation",
        PRESSURE_NAMESPACE="analytics-batch",
        PRESSURE_DEPLOYMENT="tenant-ingester",
        PLATFORM_PRIORITY_CLASS="platform-medium",
        PRODUCTION_PRIORITY_CLASS="production-critical",
        target_request_memory="512Mi",
        pressure_request_memory="2Gi",
    )
    return oracle


def _deployment(name, replicas=1, ready=1, priority_class=None, memory="512Mi"):
    return SimpleNamespace(
        metadata=SimpleNamespace(name=name),
        spec=SimpleNamespace(
            replicas=replicas,
            template=SimpleNamespace(
                spec=SimpleNamespace(
                    priority_class_name=priority_class,
                    containers=[
                        SimpleNamespace(
                            resources=SimpleNamespace(
                                requests={"memory": memory},
                            )
                        )
                    ],
                )
            ),
        ),
        status=SimpleNamespace(ready_replicas=ready),
    )


def _priority_class(name, value, global_default=False):
    return SimpleNamespace(
        metadata=SimpleNamespace(name=name),
        value=value,
        global_default=global_default,
    )


# These helpers used to return bare booleans, printing the reason and throwing
# it away. They now return a failure verdict or ``None``, so the assertions
# below check the reason as well as the outcome -- the reason is the part that
# reaches the results CSV, and an unasserted reason is an unpinned contract.


def test_a_scaled_down_shortcut_is_the_agents_doing():
    """Nothing environmental scales a Deployment to zero."""
    oracle = _oracle()
    oracle.apps_v1 = SimpleNamespace(
        list_namespaced_deployment=lambda namespace: SimpleNamespace(
            items=[
                _deployment("reservation"),
                _deployment("frontend", replicas=0, ready=0),
            ]
        )
    )

    verdict = oracle._any_deployment_unready("hotel-reservation")

    assert verdict["reason"] == "required_deployment_scaled_to_zero"
    assert verdict["failure_class"] == FailureClass.AGENT_ERROR
    assert verdict["detail"]["deployment"] == "frontend"


def test_unready_replicas_are_ambiguous_rather_than_the_agents_fault():
    """A Deployment that exists but has not converged is the Calico shape.

    Distinct from the scaled-to-zero case above: replicas are still requested,
    they just are not ready, which infrastructure produces on its own.
    """
    oracle = _oracle()
    oracle.apps_v1 = SimpleNamespace(
        list_namespaced_deployment=lambda namespace: SimpleNamespace(
            items=[
                _deployment("reservation", replicas=1, ready=1),
                _deployment("frontend", replicas=1, ready=0),
            ]
        )
    )

    verdict = oracle._any_deployment_unready("hotel-reservation")

    assert verdict["reason"] == "deployment_replicas_unready"
    assert verdict["failure_class"] == FailureClass.AMBIGUOUS
    assert verdict["detail"] == {"deployment": "frontend", "ready": 0, "desired": 1}


def test_a_memory_request_cut_is_the_agents_doing():
    oracle = _oracle()
    deployment = _deployment("reservation", memory="128Mi")

    verdict = oracle._request_was_reduced(deployment, "512Mi")

    assert verdict["reason"] == "memory_request_reduced"
    assert verdict["failure_class"] == FailureClass.AGENT_ERROR


def test_an_equal_or_larger_request_is_not_a_failure():
    oracle = _oracle()
    deployment = _deployment("tenant-ingester", memory="2Gi")

    assert oracle._request_was_reduced(deployment, "2Gi") is None


def test_request_check_is_disabled_when_injection_did_not_record_expected_memory():
    oracle = _oracle()
    deployment = _deployment("reservation", memory="128Mi")

    assert oracle._request_was_reduced(deployment, None) is None


def test_target_priority_accepts_custom_class_above_platform():
    oracle = _oracle()
    deployment = _deployment("reservation", priority_class="reservation-high-priority")
    platform = _priority_class("platform-medium", 100000)
    classes = {
        "reservation-high-priority": _priority_class("reservation-high-priority", 200000),
    }
    oracle._read_priority_class = classes.get

    assert oracle._target_priority_unsafe(deployment, platform) is None


@pytest.mark.parametrize(
    ("priority_class", "expected_reason"),
    [
        # Each is a distinct way to leave the priority relationship wrong, and
        # they used to be indistinguishable in the results.
        (None, "target_has_no_priority_class"),
        ("missing-priority", "target_priority_class_missing"),
        ("reservation-low", "target_priority_not_above_platform"),
    ],
)
def test_each_unsafe_priority_reports_its_own_reason(priority_class, expected_reason):
    oracle = _oracle()
    platform = _priority_class("platform-medium", 100000)
    classes = {
        "platform-medium": platform,
        "reservation-low": _priority_class("reservation-low", 50000),
    }
    oracle._read_priority_class = classes.get

    verdict = oracle._target_priority_unsafe(_deployment("reservation", priority_class=priority_class), platform)

    assert verdict["reason"] == expected_reason
    assert verdict["failure_class"] == FailureClass.AGENT_ERROR
