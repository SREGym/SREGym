from types import SimpleNamespace

import pytest
from kubernetes.client.rest import ApiException

from sregym.conductor.oracles.incorrect_port import IncorrectPortAssignmentMitigationOracle


def _deployment(
    *,
    address="product-catalog:8080",
    replicas=1,
    generation=2,
    observed_generation=2,
    updated=1,
    ready=1,
    available=1,
    unavailable=0,
):
    env = [SimpleNamespace(name="PRODUCT_CATALOG_ADDR", value=address)]
    pod_spec = SimpleNamespace(
        containers=[SimpleNamespace(name="checkout", env=env)],
        dns_policy="ClusterFirst",
        dns_config=SimpleNamespace(options=[SimpleNamespace(name="ndots", value="5")]),
        service_account_name="checkout",
        image_pull_secrets=[SimpleNamespace(name="registry-credentials")],
    )
    template = SimpleNamespace(
        metadata=SimpleNamespace(labels={"app.kubernetes.io/component": "checkout"}),
        spec=pod_spec,
    )
    return SimpleNamespace(
        metadata=SimpleNamespace(name="checkout", generation=generation),
        spec=SimpleNamespace(replicas=replicas, template=template),
        status=SimpleNamespace(
            observed_generation=observed_generation,
            updated_replicas=updated,
            ready_replicas=ready,
            available_replicas=available,
            unavailable_replicas=unavailable,
        ),
    )


class _CoreV1:
    def __init__(self, *, phase="Succeeded", logs='{"products": [{"id": "OLJCESPC7Z"}]}', endpoints=True):
        self.phase = phase
        self.logs = logs
        self.endpoints = endpoints
        self.created_pods = []
        self.deleted_pods = []

    def read_namespaced_endpoints(self, name, namespace):
        addresses = [SimpleNamespace(ip="10.0.0.2")] if self.endpoints else []
        return SimpleNamespace(subsets=[SimpleNamespace(addresses=addresses)])

    def create_namespaced_pod(self, namespace, body):
        self.created_pods.append((namespace, body))

    def read_namespaced_pod(self, name, namespace):
        return SimpleNamespace(status=SimpleNamespace(phase=self.phase))

    def read_namespaced_pod_log(self, name, namespace):
        return self.logs

    def delete_namespaced_pod(self, name, namespace, grace_period_seconds):
        self.deleted_pods.append((name, namespace, grace_period_seconds))


class _KubeCtl:
    def __init__(self, deployment=None, core_v1=None, get_error=None):
        self.deployment = deployment or _deployment()
        self.core_v1_api = core_v1 or _CoreV1()
        self.get_error = get_error

    def get_deployment(self, name, namespace):
        if self.get_error is not None:
            raise self.get_error
        return self.deployment


def _oracle(kubectl, *, require_source_ready=True):
    problem = SimpleNamespace(
        namespace="astronomy-shop",
        faulty_service="checkout",
        env_var="PRODUCT_CATALOG_ADDR",
        kubectl=kubectl,
    )
    oracle = IncorrectPortAssignmentMitigationOracle(problem, require_source_ready=require_source_ready)
    oracle.rollout_timeout_seconds = 0
    oracle.probe_timeout_seconds = 0
    oracle.poll_interval_seconds = 0
    return oracle


@pytest.mark.parametrize(
    "address",
    [
        "product-catalog:8080",
        "product-catalog.astronomy-shop.svc.cluster.local:8080",
        "working-catalog-alias:9090",
    ],
)
def test_accepts_any_configured_address_that_returns_a_catalog_response(address):
    core_v1 = _CoreV1()

    assert _oracle(_KubeCtl(deployment=_deployment(address=address), core_v1=core_v1)).evaluate()["success"] is True

    probe = core_v1.created_pods[0][1]
    assert address in probe.spec.containers[0].args
    assert "oteldemo.ProductCatalogService/ListProducts" in probe.spec.containers[0].args
    assert probe.metadata.labels == {"app.kubernetes.io/component": "checkout"}
    assert probe.spec.dns_policy == "ClusterFirst"
    assert probe.spec.readiness_gates[0].condition_type == "operations.example.com/serving"
    assert len(core_v1.deleted_pods) == 1


@pytest.mark.parametrize(
    ("address", "logs"),
    [
        ("bogus-host:8080", "Failed to dial target host: lookup bogus-host: no such host"),
        ("product-catalog:8082", "Failed to dial target host: connect: connection refused"),
        ("frontend:8080", "server does not support the reflection API"),
    ],
)
def test_rejects_addresses_that_do_not_answer_the_catalog_rpc(address, logs):
    core_v1 = _CoreV1(phase="Failed", logs=logs)

    assert _oracle(_KubeCtl(deployment=_deployment(address=address), core_v1=core_v1)).evaluate()["success"] is False
    assert len(core_v1.deleted_pods) == 1


def test_rejects_missing_literal_address_without_creating_probe():
    core_v1 = _CoreV1()

    assert _oracle(_KubeCtl(deployment=_deployment(address=None), core_v1=core_v1)).evaluate()["success"] is False
    assert core_v1.created_pods == []


def test_rejects_scale_to_zero_without_creating_probe():
    core_v1 = _CoreV1()
    deployment = _deployment(replicas=0, updated=0, ready=0, available=0)

    assert _oracle(_KubeCtl(deployment=deployment, core_v1=core_v1)).evaluate()["success"] is False
    assert core_v1.created_pods == []


def test_compound_address_check_can_probe_while_checkout_is_unschedulable():
    core_v1 = _CoreV1()
    deployment = _deployment(ready=0, available=0, unavailable=1)

    result = _oracle(
        _KubeCtl(deployment=deployment, core_v1=core_v1),
        require_source_ready=False,
    ).evaluate()

    assert result["success"] is True
    assert len(core_v1.created_pods) == 1


def test_rejects_stale_rollout_without_creating_probe():
    core_v1 = _CoreV1()
    deployment = _deployment(
        generation=3,
        observed_generation=2,
        updated=0,
        ready=1,
        available=1,
        unavailable=1,
    )

    assert _oracle(_KubeCtl(deployment=deployment, core_v1=core_v1)).evaluate()["success"] is False
    assert core_v1.created_pods == []


def test_rejects_checkout_without_ready_service_endpoint():
    core_v1 = _CoreV1(endpoints=False)

    assert _oracle(_KubeCtl(core_v1=core_v1)).evaluate()["success"] is False
    assert core_v1.created_pods == []


def test_rejects_deleted_checkout_deployment():
    core_v1 = _CoreV1()
    kubectl = _KubeCtl(core_v1=core_v1, get_error=ApiException(status=404))

    assert _oracle(kubectl).evaluate()["success"] is False
    assert core_v1.created_pods == []


def test_probe_timeout_is_failure_and_still_cleans_up():
    core_v1 = _CoreV1(phase="Pending")

    assert _oracle(_KubeCtl(core_v1=core_v1)).evaluate()["success"] is False
    assert len(core_v1.deleted_pods) == 1
