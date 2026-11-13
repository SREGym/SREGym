from types import SimpleNamespace
from unittest.mock import Mock

from sregym.conductor.problems.mutating_webhook_resource_limits import MutatingWebhookResourceLimits


def test_injection_respects_the_original_pods_shutdown_grace(monkeypatch):
    problem = object.__new__(MutatingWebhookResourceLimits)
    problem.namespace = "social-network"
    problem.faulty_service = "nginx-thrift"
    problem._patch_target_deployment_resources = Mock()
    problem._ensure_webhook_backend = Mock()
    problem._install_decoy_webhooks = Mock()
    problem._build_webhook_body = Mock(return_value={})
    problem.admission_api = Mock()
    problem.core_api = Mock()
    problem.core_api.list_namespaced_pod.return_value.items = [SimpleNamespace(metadata=SimpleNamespace(name="old"))]
    problem.kubectl = Mock()
    problem.kubectl.get_deployment.return_value = SimpleNamespace(
        spec=SimpleNamespace(selector=SimpleNamespace(match_labels={"service": "nginx-thrift"}))
    )
    monkeypatch.setattr("sregym.conductor.problems.mutating_webhook_resource_limits.time.sleep", lambda _: None)

    problem.inject_fault()

    deletion = problem.core_api.delete_namespaced_pod.call_args.kwargs
    assert deletion["name"] == "old"
    assert deletion["body"].grace_period_seconds is None
