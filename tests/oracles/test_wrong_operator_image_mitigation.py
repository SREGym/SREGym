"""The wrong-operator-image oracle also checks the operator's own namespace.

The fault breaks the TiDB operator in ``tidb-operator``; the TiDB cluster in the
problem namespace keeps running, so checking only that namespace passed with the
fault live.
"""

from types import SimpleNamespace

import pytest

from sregym.conductor.oracles import mitigation
from sregym.conductor.oracles.operator_misoperation.wrong_operator_image_mitigation import (
    OPERATOR_NAMESPACE,
    WrongOperatorImageMitigationOracle,
)


@pytest.fixture(autouse=True)
def _fast_rollout_settle(monkeypatch):
    monkeypatch.setattr(mitigation, "_ROLLOUT_SETTLE_SECONDS", 0.05)
    monkeypatch.setattr(mitigation, "_ROLLOUT_POLL_INTERVAL", 0.01)


def _deployment(name, *, ready=1):
    return SimpleNamespace(
        metadata=SimpleNamespace(generation=1, name=name),
        spec=SimpleNamespace(replicas=1),
        status=SimpleNamespace(
            replicas=1,
            observed_generation=1,
            available_replicas=ready,
            updated_replicas=1,
            ready_replicas=ready,
            unavailable_replicas=1 - ready,
        ),
    )


def _pod(name, *, waiting=None):
    state = SimpleNamespace(waiting=SimpleNamespace(reason=waiting) if waiting else None, terminated=None)
    return SimpleNamespace(
        metadata=SimpleNamespace(name=name),
        status=SimpleNamespace(
            phase="Running",
            container_statuses=[SimpleNamespace(name="c", ready=waiting is None, state=state)],
        ),
    )


class _KubeCtl:
    def __init__(self):
        self.deployments = {"tidb-cluster": [_deployment("tidb-app")], OPERATOR_NAMESPACE: []}
        self.pods = {"tidb-cluster": [_pod("tidb-app-1")], OPERATOR_NAMESPACE: []}

    def operator(self, *, ready=1, waiting=None):
        self.deployments[OPERATOR_NAMESPACE] = [_deployment("tidb-controller-manager", ready=ready)]
        self.pods[OPERATOR_NAMESPACE] = [_pod("tidb-controller-manager-1", waiting=waiting)]

    def list_deployments(self, namespace):
        return SimpleNamespace(items=list(self.deployments.get(namespace, [])))

    def list_pods(self, namespace):
        return SimpleNamespace(items=list(self.pods.get(namespace, [])))


@pytest.fixture
def kubectl():
    return _KubeCtl()


@pytest.fixture
def oracle(kubectl):
    problem = SimpleNamespace(kubectl=kubectl, namespace="tidb-cluster")
    oracle = WrongOperatorImageMitigationOracle(problem)
    kubectl.operator()
    oracle.capture_baseline()
    return oracle


def test_healthy_operator_and_cluster_pass(oracle):
    assert oracle.evaluate()["success"] is True


def test_operator_on_a_missing_image_fails(oracle, kubectl):
    kubectl.operator(ready=0, waiting="ImagePullBackOff")
    verdict = oracle.evaluate()
    assert verdict["success"] is False
    assert verdict["reason"] == "deployment_replicas_unready"
    assert verdict["detail"]["namespace"] == OPERATOR_NAMESPACE


def test_deleting_the_operator_fails(oracle, kubectl):
    kubectl.deployments[OPERATOR_NAMESPACE] = []
    kubectl.pods[OPERATOR_NAMESPACE] = []
    verdict = oracle.evaluate()
    assert verdict["success"] is False
    assert verdict["reason"] == "required_deployment_missing"


def test_an_unready_operator_pod_fails_even_if_the_deployment_looks_ready(oracle, kubectl):
    kubectl.operator(ready=1, waiting="ErrImagePull")
    assert oracle.evaluate()["reason"] == "pods_not_ready"
