from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from kubernetes import client

from sregym.conductor.problems.search_rate_retry_collapse import SearchRateRetryCollapse


class _AppsV1:
    def __init__(self):
        self.replaced = None

    def replace_namespaced_deployment(self, name, namespace, body):
        self.replaced = (name, namespace, body)


def test_problem_disables_the_unrelated_default_application_workload():
    assert SearchRateRetryCollapse.run_default_workload is False


def test_trigger_is_long_enough_for_a_fresh_application_deployment():
    assert SearchRateRetryCollapse.trigger_seconds == 10.0


def test_vulnerable_policy_is_part_of_the_initial_deployment():
    overrides = SearchRateRetryCollapse._vulnerable_deployment_env()

    assert overrides["rate"]["hotel-reserv-rate"]["RATE_BACKEND_QPS_LIMIT"] == "20"
    assert overrides["search"]["hotel-reserv-search"]["RATE_RPC_MAX_ATTEMPTS"] == "3"


def test_rollout_wait_ignores_unrelated_agent_pods():
    problem = SearchRateRetryCollapse.__new__(SearchRateRetryCollapse)
    problem.namespace = "hotel-reservation"
    problem.kubectl = SimpleNamespace(
        exec_command_checked=Mock(),
        wait_for_ready=Mock(),
    )

    problem._wait_for_rollouts()

    problem.kubectl.wait_for_ready.assert_called_once_with(
        "hotel-reservation",
        service_names=["rate", "search"],
    )


def test_replace_container_env_preserves_unrelated_duplicate_entries():
    container = SimpleNamespace(
        name="hotel-reserv-search",
        env=[
            client.V1EnvVar(name="DUPLICATE", value="first"),
            client.V1EnvVar(name="RATE_RPC_MAX_ATTEMPTS", value="1"),
            client.V1EnvVar(name="DUPLICATE", value="second"),
        ],
    )
    deployment = SimpleNamespace(
        spec=SimpleNamespace(template=SimpleNamespace(spec=SimpleNamespace(containers=[container])))
    )
    apps_v1 = _AppsV1()
    problem = SearchRateRetryCollapse.__new__(SearchRateRetryCollapse)
    problem.namespace = "hotel-reservation"
    problem.kubectl = SimpleNamespace(
        get_deployment=lambda *_: deployment,
        apps_v1_api=apps_v1,
    )

    problem._replace_container_env(
        "search",
        "hotel-reserv-search",
        {"RATE_RPC_MAX_ATTEMPTS": "3"},
    )

    replaced = apps_v1.replaced[2]
    env = replaced.spec.template.spec.containers[0].env
    assert [(item.name, item.value) for item in env] == [
        ("DUPLICATE", "first"),
        ("DUPLICATE", "second"),
        ("RATE_RPC_MAX_ATTEMPTS", "3"),
    ]


def test_repeated_injection_fails_before_mutating_cluster():
    problem = SearchRateRetryCollapse.__new__(SearchRateRetryCollapse)
    problem.fault_injected = False
    problem._injection_attempted = True

    with pytest.raises(RuntimeError, match="already active"):
        problem.inject_fault()


def test_injection_only_runs_the_protected_workload_and_trigger():
    problem = SearchRateRetryCollapse.__new__(SearchRateRetryCollapse)
    problem.fault_injected = False
    problem._injection_attempted = False
    problem.workload = SimpleNamespace(start=Mock(), stop=Mock())
    problem._establish_healthy_vulnerable_baseline = Mock()
    problem._apply_trigger_and_verify_sustaining_loop = Mock()

    problem.inject_fault()

    problem.workload.start.assert_called_once_with()
    problem._establish_healthy_vulnerable_baseline.assert_called_once_with()
    problem._apply_trigger_and_verify_sustaining_loop.assert_called_once_with()
    assert problem.fault_injected is True


def test_recovery_applies_an_intentional_safe_policy():
    problem = SearchRateRetryCollapse.__new__(SearchRateRetryCollapse)
    problem.fault_injected = True
    problem._apply_mitigated_policy = Mock()
    problem._wait_for_rollouts = Mock()
    problem.workload = SimpleNamespace(stop=Mock())

    problem.recover_fault()

    problem._apply_mitigated_policy.assert_called_once_with()
    problem._wait_for_rollouts.assert_called_once_with()
    problem.workload.stop.assert_called_once_with()
    assert problem.fault_injected is False
