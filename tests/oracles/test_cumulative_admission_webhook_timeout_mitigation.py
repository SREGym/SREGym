from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from kubernetes import client
from kubernetes.client.exceptions import ApiException

from sregym.conductor.oracles import cumulative_admission_webhook_timeout_mitigation as module


def deployment(name, replicas=1):
    return client.V1Deployment(
        metadata=client.V1ObjectMeta(name=name, generation=1),
        spec=client.V1DeploymentSpec(
            replicas=replicas,
            selector=client.V1LabelSelector(match_labels={"app": name}),
            template=client.V1PodTemplateSpec(spec=client.V1PodSpec(containers=[])),
        ),
        status=client.V1DeploymentStatus(
            observed_generation=1,
            replicas=replicas,
            updated_replicas=replicas,
            ready_replicas=replicas,
            available_replicas=replicas,
        ),
    )


@pytest.fixture
def cluster(monkeypatch):
    core, apps, networking, admission = (Mock() for _ in range(4))
    for name, api in (
        ("CoreV1Api", core),
        ("AppsV1Api", apps),
        ("NetworkingV1Api", networking),
        ("AdmissionregistrationV1Api", admission),
    ):
        monkeypatch.setattr(module.client, name, lambda api=api: api)
    deployments = {name: deployment(name) for name in ("frontend", "recommendation")}
    services = {name: client.V1Service(metadata=client.V1ObjectMeta(name=name)) for name in deployments}
    kube = SimpleNamespace(
        list_deployments=lambda _: SimpleNamespace(items=list(deployments.values())),
        list_pods=lambda _: SimpleNamespace(
            items=[
                client.V1Pod(
                    metadata=client.V1ObjectMeta(name="recommendation-pod"),
                    spec=client.V1PodSpec(containers=[]),
                    status=client.V1PodStatus(phase="Running", container_statuses=[]),
                )
            ]
        ),
        core_v1_api=core,
        apps_v1_api=apps,
    )
    core.list_namespaced_service.side_effect = lambda _: SimpleNamespace(items=list(services.values()))
    core.list_namespaced_persistent_volume_claim.return_value = SimpleNamespace(items=[])
    apps.list_namespaced_stateful_set.return_value = SimpleNamespace(items=[])
    apps.read_namespaced_deployment.side_effect = lambda name, namespace: deployments[name]
    core.read_namespaced_endpoints.return_value = SimpleNamespace(
        subsets=[SimpleNamespace(addresses=["recommendation-pod"])]
    )
    networking.list_namespaced_network_policy.return_value = SimpleNamespace(
        items=[
            client.V1NetworkPolicy(
                spec=client.V1NetworkPolicySpec(pod_selector=client.V1LabelSelector(), policy_types=["Ingress"])
            )
        ]
    )
    problem = SimpleNamespace(
        namespace="hotel-reservation",
        TARGET_DEPLOYMENT="recommendation",
        POLICY_NAMESPACE="policy-system",
        WEBHOOK_BACKEND_NAMES=("audit-log-enforcer",),
        BACKEND_TIER_LABEL={"tier": "compliance"},
        kubectl=kube,
    )
    oracle = module.CumulativeAdmissionWebhookTimeoutMitigationOracle(problem)
    oracle.capture_baseline()
    # Exercise the real shared health checks without a rollout delay.
    oracle.rollout_time = 0
    oracle._functional_probe = Mock(return_value=(True, "admitted"))
    return SimpleNamespace(
        oracle=oracle,
        deployments=deployments,
        services=services,
        core=core,
        apps=apps,
        networking=networking,
        admission=admission,
    )


def test_healthy_application_still_requires_fresh_admission(cluster):
    assert cluster.oracle.evaluate() == {"success": True}
    cluster.oracle._functional_probe.assert_called_once_with()


@pytest.mark.parametrize("name", ["frontend", "recommendation"])
def test_rejects_required_deployment_scaled_to_zero(cluster, name):
    cluster.deployments[name] = deployment(name, replicas=0)
    result = cluster.oracle.evaluate()
    assert result["reason"] == "required_deployment_scaled_to_zero"
    assert result["failure_class"] == "agent_error"
    assert result["detail"]["deployment"] == name
    cluster.oracle._functional_probe.assert_not_called()


def test_rejects_deleted_frontend(cluster):
    del cluster.deployments["frontend"]
    assert cluster.oracle.evaluate()["reason"] == "required_deployment_missing"


def test_rejects_stale_frontend_rollout(cluster):
    cluster.deployments["frontend"].metadata.generation = 2
    assert cluster.oracle.evaluate()["reason"] == "deployment_replicas_unready"


def test_rejects_deleted_frontend_service(cluster):
    del cluster.services["frontend"]
    assert cluster.oracle.evaluate()["reason"] == "required_resource_missing"


@pytest.mark.parametrize("check", ["webhooks", "isolation", "admission"])
def test_shared_health_does_not_replace_fault_specific_checks(cluster, check):
    if check == "webhooks":
        cluster.admission.read_mutating_webhook_configuration.side_effect = ApiException(status=404)
        reason = "workload_not_intact"
    elif check == "isolation":
        cluster.networking.list_namespaced_network_policy.return_value = SimpleNamespace(items=[])
        reason = "network_isolation_removed"
    else:
        cluster.oracle._functional_probe.return_value = (False, "admission timed out")
        reason = "admission_probe_failed"
    assert cluster.oracle.evaluate()["reason"] == reason


def test_shared_api_failure_is_not_reported_as_health(cluster):
    cluster.core.list_namespaced_service.side_effect = ApiException(status=503)
    with pytest.raises(ApiException, match="503"):
        cluster.oracle.evaluate()
