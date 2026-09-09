"""Tests for the pod-local hosts-override problem on Astronomy Shop.

The unit tests pin down the injection contract without a cluster: the patch has
to name `frontend` (the hostname the frontend-proxy actually dials), and
recovery has to remove it. The integration test runs the real
inject -> oracle fails -> recover -> oracle passes lifecycle and is skipped
unless `-m integration` is selected against a live cluster.
"""

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from sregym.conductor.problems.stale_hostaliases_dns_poisoning_astronomy_shop import (
    StaleHostAliasesDNSPoisoningAstronomyShop,
)
from sregym.generators.fault.inject_virtual import VirtualizationFaultInjector
from sregym.paths import TARGET_MICROSERVICES

NAMESPACE = "astronomy-shop"
DEPLOYMENT = "frontend-proxy"
TARGET_BACKEND = "frontend"

_CHART = TARGET_MICROSERVICES / "astronomy-shop" / "charts" / "opentelemetry-demo"
_KUBECONFIG = Path(os.environ.get("KUBECONFIG") or Path.home() / ".kube" / "config")
_HAS_CLUSTER = _CHART.exists() and _KUBECONFIG.exists()


class _AppsV1:
    def __init__(self):
        self.patches = []

    def patch_namespaced_deployment(self, name, namespace, body):
        self.patches.append((name, namespace, body))


class _KubeCtl:
    def __init__(self, host_aliases=None, rollout_fails=False):
        self.apps_v1_api = _AppsV1()
        self.commands = []
        self.rollout_fails = rollout_fails
        self.deployment = SimpleNamespace(
            spec=SimpleNamespace(template=SimpleNamespace(spec=SimpleNamespace(host_aliases=host_aliases)))
        )

    def get_deployment(self, name, namespace):
        return self.deployment

    def exec_command(self, command):
        self.commands.append(command)
        return f'deployment "{DEPLOYMENT}" successfully rolled out'

    def exec_command_checked(self, command, input_data=None, timeout=None):
        self.commands.append(command)
        if self.rollout_fails:
            raise RuntimeError(f"Command failed (exit 1): {command}: error: timed out")
        return f'deployment "{DEPLOYMENT}" successfully rolled out'


def _injector(kubectl):
    injector = VirtualizationFaultInjector.__new__(VirtualizationFaultInjector)
    injector.namespace = NAMESPACE
    injector.kubectl = kubectl
    return injector


def test_injection_patches_the_hostname_the_proxy_actually_dials():
    kubectl = _KubeCtl()
    _injector(kubectl).inject_stale_hostaliases(
        microservices=[DEPLOYMENT],
        target_host=TARGET_BACKEND,
        blackhole_ip="127.0.0.1",
    )

    name, namespace, patch = kubectl.apps_v1_api.patches[0]
    assert (name, namespace) == (DEPLOYMENT, NAMESPACE)
    assert patch == [
        {
            "op": "add",
            "path": "/spec/template/spec/hostAliases",
            "value": [{"ip": "127.0.0.1", "hostnames": [TARGET_BACKEND]}],
        }
    ]


def test_injection_waits_for_the_rollout_instead_of_sleeping():
    kubectl = _KubeCtl()
    _injector(kubectl).inject_stale_hostaliases(
        microservices=[DEPLOYMENT],
        target_host=TARGET_BACKEND,
        blackhole_ip="127.0.0.1",
    )

    assert any("rollout status" in command for command in kubectl.commands)


def test_injection_raises_on_rollout_timeout():
    """A failed rollout must propagate so setup does not continue on a broken cluster."""
    kubectl = _KubeCtl(rollout_fails=True)
    with pytest.raises(RuntimeError, match="timed out"):
        _injector(kubectl).inject_stale_hostaliases(
            microservices=[DEPLOYMENT],
            target_host=TARGET_BACKEND,
            blackhole_ip="127.0.0.1",
        )


def test_recovery_removes_the_override_and_waits():
    kubectl = _KubeCtl(host_aliases=[SimpleNamespace(ip="127.0.0.1", hostnames=[TARGET_BACKEND])])
    _injector(kubectl).recover_stale_hostaliases(microservices=[DEPLOYMENT])

    _, _, patch = kubectl.apps_v1_api.patches[0]
    assert patch == [{"op": "remove", "path": "/spec/template/spec/hostAliases"}]
    assert any("rollout status" in command for command in kubectl.commands)


def test_recovery_is_a_noop_when_the_agent_already_removed_the_override():
    """A JSON-patch `remove` on an absent path 422s; recovery must not depend on it."""
    kubectl = _KubeCtl(host_aliases=None)
    _injector(kubectl).recover_stale_hostaliases(microservices=[DEPLOYMENT])

    assert kubectl.apps_v1_api.patches == []


def test_problem_targets_the_charts_backend_hostname():
    """`frontend-proxy` targets the `frontend` service as configured in values.yaml."""
    assert StaleHostAliasesDNSPoisoningAstronomyShop.FAULTY_SERVICE == DEPLOYMENT
    assert StaleHostAliasesDNSPoisoningAstronomyShop.TARGET_BACKEND == TARGET_BACKEND
    assert StaleHostAliasesDNSPoisoningAstronomyShop.BLACKHOLE_IP == "127.0.0.1"


def test_backend_hostname_matches_the_proxy_env_in_the_chart():
    """The alias only bites if it spells the host in FRONTEND_HOST exactly."""
    frontend_host = "frontend"  # values.yaml -> components.frontend-proxy.env
    assert frontend_host == StaleHostAliasesDNSPoisoningAstronomyShop.TARGET_BACKEND


@pytest.mark.integration
@pytest.mark.skipif(not _HAS_CLUSTER, reason="no kubeconfig or astronomy-shop chart submodule on disk")
def test_lifecycle_against_a_live_cluster():
    """inject -> probe fails -> oracle fails -> recover -> probe passes -> oracle passes."""
    problem = StaleHostAliasesDNSPoisoningAstronomyShop()
    problem.app.deploy()
    problem.kubectl.wait_for_ready(problem.namespace)
    problem.mitigation_oracle.capture_baseline()

    try:
        # Pre-injection: traffic must work and oracle must pass.
        assert problem.mitigation_oracle._run_product_probe(), "pre-injection probe must succeed"
        assert problem.mitigation_oracle.evaluate()["success"] is True, "healthy cluster should pass"

        # Inject the fault.
        problem.inject_fault()
        deployment = problem.kubectl.get_deployment(problem.faulty_service, problem.namespace)
        aliases = deployment.spec.template.spec.host_aliases
        assert aliases and aliases[0].ip == problem.blackhole_ip
        assert problem.target_backend in aliases[0].hostnames

        # Post-injection: traffic must fail and oracle must fail.
        assert not problem.mitigation_oracle._run_product_probe(), "post-injection probe must fail"
        assert problem.mitigation_oracle.evaluate()["success"] is False, "the oracle must fail while the fault is live"

        # Recover.
        problem.recover_fault()
        deployment = problem.kubectl.get_deployment(problem.faulty_service, problem.namespace)
        assert not deployment.spec.template.spec.host_aliases

        # Post-recovery: traffic must work and oracle must pass.
        assert problem.mitigation_oracle._run_product_probe(), "post-recovery probe must succeed"
        assert problem.mitigation_oracle.evaluate()["success"] is True, (
            "the oracle must pass once the override is removed"
        )
    finally:
        problem.app.cleanup()
