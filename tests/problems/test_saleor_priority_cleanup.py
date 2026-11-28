from types import SimpleNamespace
from unittest.mock import Mock, patch

from sregym.conductor.problems.apps.saleor.resources import PriorityPreemptionCascadeSaleor


def test_cleanup_runs_support_and_application_cleanup_once():
    application_cleanup = Mock()
    app = SimpleNamespace(namespace="saleor", cleanup=application_cleanup, create_workload=Mock())
    module = "sregym.conductor.problems.priority_preemption_cascade"
    with (
        patch("sregym.conductor.problems.apps.saleor.resources.make_app", return_value=app),
        patch("sregym.conductor.problems.apps.saleor.resources.configure_port"),
        patch(f"{module}.KubeCtl"),
        patch(f"{module}.client.AppsV1Api"),
        patch(f"{module}.client.CoreV1Api"),
        patch(f"{module}.client.SchedulingV1Api"),
        patch(f"{module}.LLMAsAJudgeOracle"),
    ):
        problem = PriorityPreemptionCascadeSaleor()
    problem._delete_support_resources = Mock()

    problem.app.cleanup()

    problem._delete_support_resources.assert_called_once_with()
    application_cleanup.assert_called_once_with()


def test_saleor_priority_preemption_sets_liveness_threshold_and_timeout():
    problem = object.__new__(PriorityPreemptionCascadeSaleor)
    assert problem.DEPLOYMENT_READY_TIMEOUT_S == 300

    with_liveness = SimpleNamespace(liveness_probe=SimpleNamespace(failure_threshold=3))
    patch_dict = problem._extra_target_container_patch(with_liveness)
    assert patch_dict == {"livenessProbe": {"failureThreshold": 12}}
    assert "startupProbe" not in patch_dict

    without_liveness = SimpleNamespace(liveness_probe=None)
    assert problem._extra_target_container_patch(without_liveness) == {}


def test_saleor_priority_preemption_configures_180s_window_and_baseline():
    app = SimpleNamespace(namespace="saleor", cleanup=Mock(), create_workload=Mock())
    cascade_mod = "sregym.conductor.problems.priority_preemption_cascade"
    env_mod = "sregym.conductor.problems.environment"
    saleor_mod = "sregym.conductor.problems.apps.saleor.resources"
    with (
        patch(f"{saleor_mod}.make_app", return_value=app),
        patch(f"{saleor_mod}.client.AppsV1Api"),
        patch(f"{saleor_mod}.client.CoreV1Api"),
        patch(f"{saleor_mod}.client.SchedulingV1Api"),
        patch(f"{cascade_mod}.KubeCtl"),
        patch(f"{cascade_mod}.client.AppsV1Api"),
        patch(f"{cascade_mod}.client.CoreV1Api"),
        patch(f"{cascade_mod}.client.SchedulingV1Api"),
        patch(f"{cascade_mod}.LLMAsAJudgeOracle"),
        patch(f"{env_mod}.KubeCtl"),
        patch(f"{env_mod}.LLMAsAJudgeOracle"),
    ):
        problem = PriorityPreemptionCascadeSaleor()

    assert problem.baseline_duration_s == 180
    assert problem.mitigation_oracle.oracles["app_health"].window_s == 180.0
