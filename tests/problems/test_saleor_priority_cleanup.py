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
