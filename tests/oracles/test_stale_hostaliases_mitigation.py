"""Unit tests for StaleHostAliasesMitigationOracle.

These run without a cluster: the oracle is driven against fake kubectl objects
so every rejection path is exercised, including the two failures that a
health-only oracle misses (a live pod still carrying the override, and a
frontend that is Ready but cannot fetch the catalog).
"""

from types import SimpleNamespace

import pytest

from sregym.conductor.oracles.stale_hostaliases_mitigation import StaleHostAliasesMitigationOracle

NAMESPACE = "astronomy-shop"
DEPLOYMENT = "frontend"
BACKEND = "product-catalog"


def _deployment(host_aliases=None, replicas=1, ready=1):
    return SimpleNamespace(
        metadata=SimpleNamespace(name=DEPLOYMENT, generation=2),
        spec=SimpleNamespace(
            replicas=replicas,
            selector=SimpleNamespace(match_labels={"app": DEPLOYMENT}),
            template=SimpleNamespace(spec=SimpleNamespace(host_aliases=host_aliases)),
        ),
        status=SimpleNamespace(
            observed_generation=2,
            replicas=replicas,
            updated_replicas=ready,
            ready_replicas=ready,
            available_replicas=ready,
            unavailable_replicas=0,
        ),
    )


def _pod(name, host_aliases=None, labels=None, deleting=False):
    return SimpleNamespace(
        metadata=SimpleNamespace(
            name=name,
            labels={"app": DEPLOYMENT} if labels is None else labels,
            deletion_timestamp="2026-01-01T00:00:00Z" if deleting else None,
        ),
        spec=SimpleNamespace(host_aliases=host_aliases),
    )


def _alias(ip, hostnames):
    return SimpleNamespace(ip=ip, hostnames=hostnames)


class _CoreV1:
    def __init__(self, endpoints_ready=True):
        self.endpoints_ready = endpoints_ready

    def read_namespaced_endpoints(self, name, namespace):
        addresses = [SimpleNamespace()] if self.endpoints_ready else []
        return SimpleNamespace(subsets=[SimpleNamespace(addresses=addresses)])


class _KubeCtl:
    def __init__(self, deployment, pods=(), endpoints_ready=True, missing=False):
        self.deployment = deployment
        self.pods = list(pods)
        self.missing = missing
        self.core_v1_api = _CoreV1(endpoints_ready)

    def get_deployment(self, name, namespace):
        if self.missing:
            raise RuntimeError("deployments.apps 'frontend' not found")
        return self.deployment

    def list_pods(self, namespace):
        return SimpleNamespace(items=self.pods)


def _oracle(kubectl, probe_ok=True):
    problem = SimpleNamespace(
        kubectl=kubectl,
        namespace=NAMESPACE,
        faulty_service=DEPLOYMENT,
        target_backend=BACKEND,
    )
    oracle = StaleHostAliasesMitigationOracle(problem)
    oracle.rollout_timeout_seconds = 0
    oracle._run_product_probe = lambda: probe_ok
    return oracle


def test_passes_when_override_gone_and_catalog_serves():
    kubectl = _KubeCtl(_deployment(), pods=[_pod("frontend-new")])
    result = _oracle(kubectl).evaluate()

    assert result["success"] is True
    assert result["template_override_removed"] is True
    assert result["no_pod_carries_override"] is True
    assert result["product_probe_succeeded"] is True


def test_fails_while_fault_is_live():
    """The unmitigated cluster must not pass — this is what PR #944 got wrong."""
    aliases = [_alias("127.0.0.1", [BACKEND])]
    kubectl = _KubeCtl(_deployment(host_aliases=aliases), pods=[_pod("frontend-old", aliases)])
    result = _oracle(kubectl).evaluate()

    assert result["success"] is False
    assert result["template_override_removed"] is False
    assert "still overrides" in result["reason"]


def test_fails_when_a_stale_pod_still_carries_the_override():
    """Template clean but an old pod survived: /etc/hosts is still poisoned there."""
    kubectl = _KubeCtl(
        _deployment(),
        pods=[_pod("frontend-new"), _pod("frontend-old", [_alias("127.0.0.1", [BACKEND])])],
    )
    result = _oracle(kubectl).evaluate()

    assert result["success"] is False
    assert result["template_override_removed"] is True
    assert result["no_pod_carries_override"] is False
    assert "frontend-old" in result["reason"]


def test_ignores_terminating_pods():
    kubectl = _KubeCtl(
        _deployment(),
        pods=[_pod("frontend-new"), _pod("frontend-old", [_alias("127.0.0.1", [BACKEND])], deleting=True)],
    )
    assert _oracle(kubectl).evaluate()["success"] is True


def test_ignores_pods_outside_the_deployment():
    kubectl = _KubeCtl(
        _deployment(),
        pods=[
            _pod("frontend-new"),
            _pod("checkout-1", [_alias("127.0.0.1", [BACKEND])], labels={"app": "checkout"}),
        ],
    )
    assert _oracle(kubectl).evaluate()["success"] is True


def test_fails_when_alias_is_merely_repointed():
    """Hardcoding the current ClusterIP is still an override, not a fix."""
    aliases = [_alias("10.96.4.7", [BACKEND])]
    kubectl = _KubeCtl(_deployment(host_aliases=aliases), pods=[_pod("frontend-new", aliases)])
    assert _oracle(kubectl).evaluate()["success"] is False


@pytest.mark.parametrize(
    "hostname",
    [
        BACKEND,
        f"{BACKEND}.{NAMESPACE}",
        f"{BACKEND}.{NAMESPACE}.svc",
        f"{BACKEND}.{NAMESPACE}.svc.cluster.local",
    ],
)
def test_detects_every_qualified_form_of_the_backend_name(hostname):
    aliases = [_alias("127.0.0.1", [hostname])]
    kubectl = _KubeCtl(_deployment(host_aliases=aliases), pods=[_pod("frontend-new", aliases)])
    assert _oracle(kubectl).evaluate()["success"] is False


def test_allows_unrelated_host_aliases():
    aliases = [_alias("10.0.0.9", ["legacy-billing.internal"])]
    kubectl = _KubeCtl(_deployment(host_aliases=aliases), pods=[_pod("frontend-new", aliases)])
    assert _oracle(kubectl).evaluate()["success"] is True


def test_fails_when_catalog_requests_still_error():
    """Config is clean and the pod is Ready, but /api/products does not serve."""
    kubectl = _KubeCtl(_deployment(), pods=[_pod("frontend-new")])
    result = _oracle(kubectl, probe_ok=False).evaluate()

    assert result["success"] is False
    assert result["no_pod_carries_override"] is True
    assert result["product_probe_succeeded"] is False


def test_fails_when_deployment_is_scaled_to_zero():
    kubectl = _KubeCtl(_deployment(replicas=0, ready=0))
    result = _oracle(kubectl).evaluate()

    assert result["success"] is False
    assert "scaled to zero" in result["reason"]


def test_fails_when_deployment_is_deleted():
    kubectl = _KubeCtl(_deployment(), missing=True)
    result = _oracle(kubectl).evaluate()

    assert result["success"] is False
    assert result["deployment_exists"] is False


def test_fails_when_rollout_never_completes():
    kubectl = _KubeCtl(_deployment(replicas=2, ready=1), pods=[_pod("frontend-new")])
    result = _oracle(kubectl).evaluate()

    assert result["success"] is False
    assert result["rollout_complete"] is False


def test_fails_when_service_has_no_ready_endpoint():
    kubectl = _KubeCtl(_deployment(), pods=[_pod("frontend-new")], endpoints_ready=False)
    result = _oracle(kubectl).evaluate()

    assert result["success"] is False
    assert result["ready_endpoint"] is False
