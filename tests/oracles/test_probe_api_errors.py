"""Exercise probe errors through evaluate(), including the real probe helper."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from kubernetes import client
from kubernetes.client.rest import ApiException

from sregym.conductor.oracles.dns_resolution_mitigation import DNSResolutionMitigationOracle
from sregym.conductor.oracles.incorrect_port import IncorrectPortAssignmentMitigationOracle
from sregym.conductor.oracles.namespace_memory_limit_mitigation import NamespaceMemoryLimitMitigationOracle
from sregym.conductor.oracles.network_policy_oracle import NetworkPolicyMitigationOracle


@pytest.fixture(
    params=[
        (NamespaceMemoryLimitMitigationOracle, "SEARCH_OK"),
        (DNSResolutionMitigationOracle, "DNS_OK"),
        (NetworkPolicyMitigationOracle, "RECOMMENDATION_OK"),
        (IncorrectPortAssignmentMitigationOracle, '{"products": [{"id": "OLJCESPC7Z"}]}'),
    ],
    ids=["namespace-memory", "dns", "network-policy", "incorrect-port"],
)
def probe_oracle(request):
    oracle_class, logs = request.param
    labels = {"app": "target"}
    pod = client.V1Pod(metadata=client.V1ObjectMeta(name="target-pod", labels=labels))
    deployment = client.V1Deployment(
        metadata=client.V1ObjectMeta(name="target", generation=1),
        spec=client.V1DeploymentSpec(
            replicas=1,
            selector=client.V1LabelSelector(match_labels=labels),
            template=client.V1PodTemplateSpec(
                metadata=client.V1ObjectMeta(labels=labels),
                spec=client.V1PodSpec(
                    dns_policy="ClusterFirst",
                    containers=[
                        client.V1Container(
                            name="target",
                            env=[client.V1EnvVar(name="PRODUCT_CATALOG_ADDR", value="product-catalog:8080")],
                        )
                    ],
                ),
            ),
        ),
        status=client.V1DeploymentStatus(
            observed_generation=1,
            replicas=1,
            updated_replicas=1,
            ready_replicas=1,
            available_replicas=1,
            unavailable_replicas=0,
        ),
    )
    service = client.V1Service(
        metadata=client.V1ObjectMeta(name="target"),
        spec=client.V1ServiceSpec(selector=labels, ports=[client.V1ServicePort(port=8080)]),
    )
    core = Mock(spec=client.CoreV1Api)
    core.read_namespaced_service.return_value = service
    core.read_namespaced_endpoints.return_value = SimpleNamespace(
        subsets=[
            SimpleNamespace(
                addresses=[
                    SimpleNamespace(
                        target_ref=client.V1ObjectReference(kind="Pod", name=pod.metadata.name),
                    )
                ]
            )
        ],
    )
    core.read_namespaced_pod.return_value = client.V1Pod(status=client.V1PodStatus(phase="Succeeded"))
    core.read_namespaced_pod_log.return_value = logs
    kubectl = SimpleNamespace(
        core_v1_api=core,
        get_deployment=Mock(return_value=deployment),
        list_pods=Mock(return_value=SimpleNamespace(items=[pod])),
        list_services=Mock(return_value=SimpleNamespace(items=[service])),
        get_resource_quotas=Mock(return_value=[]),
    )
    oracle = oracle_class(
        SimpleNamespace(
            namespace="app",
            faulty_service="target",
            env_var="PRODUCT_CATALOG_ADDR",
            kubectl=kubectl,
            app=SimpleNamespace(frontend_service="frontend", frontend_port=5000),
        )
    )
    return oracle, core


def _assert_probe_cleanup(core):
    core.create_namespaced_pod.assert_called_once()
    created = core.create_namespaced_pod.call_args.kwargs["body"]
    core.delete_namespaced_pod.assert_called_once_with(
        name=created.metadata.name,
        namespace="app",
        grace_period_seconds=0,
    )


@pytest.mark.parametrize("operation", ["create_namespaced_pod", "read_namespaced_pod", "read_namespaced_pod_log"])
@pytest.mark.parametrize(
    ("status", "reason", "category"),
    [
        (503, "kubernetes_api_error", "environment_error"),
        (403, "kubernetes_request_failed", "ambiguous"),
        (404, "kubernetes_resource_missing", "ambiguous"),
    ],
)
def test_probe_api_error_reaches_shared_classifier(probe_oracle, operation, status, reason, category):
    oracle, core = probe_oracle
    getattr(core, operation).side_effect = ApiException(status=status, reason="probe request failed")
    # A missing probe during cleanup must not replace the original exception.
    core.delete_namespaced_pod.side_effect = ApiException(status=404)

    result = oracle.evaluate()

    assert result["success"] is False
    assert result["reason"] == reason
    assert result["failure_class"] == category
    assert result["detail"]["status"] == status
    _assert_probe_cleanup(core)
    if operation == "create_namespaced_pod":
        core.read_namespaced_pod.assert_not_called()
    if operation != "read_namespaced_pod_log":
        core.read_namespaced_pod_log.assert_not_called()


@pytest.mark.parametrize("phase", ["Succeeded", "Failed"])
def test_completed_probe_keeps_its_existing_verdict_and_cleanup(probe_oracle, phase):
    oracle, core = probe_oracle
    core.read_namespaced_pod.return_value.status.phase = phase

    result = oracle.evaluate()

    assert result["success"] is (phase == "Succeeded")
    if phase == "Failed":
        assert result["failure_class"] == "agent_error"
    else:
        assert "failure_class" not in result
    _assert_probe_cleanup(core)
