from types import SimpleNamespace

import pytest

from sregym.conductor.oracles.cpu_throttling_mitigation import CpuThrottlingMitigationOracle


def _deployment(
    name,
    *,
    replicas=1,
    ready=None,
    updated=None,
    available=None,
    cpu_limit="1",
    generation=1,
    observed_generation=1,
):
    ready = replicas if ready is None else ready
    updated = replicas if updated is None else updated
    available = replicas if available is None else available
    return SimpleNamespace(
        metadata=SimpleNamespace(name=name, generation=generation),
        spec=SimpleNamespace(
            replicas=replicas,
            template=SimpleNamespace(
                spec=SimpleNamespace(
                    containers=[
                        SimpleNamespace(
                            name=f"{name}-container",
                            resources=SimpleNamespace(limits={} if cpu_limit is None else {"cpu": cpu_limit}),
                        )
                    ]
                )
            ),
        ),
        status=SimpleNamespace(
            observed_generation=observed_generation,
            updated_replicas=updated,
            ready_replicas=ready,
            available_replicas=available,
            unavailable_replicas=max(0, replicas - available),
        ),
    )


class _KubeCtl:
    def __init__(self, deployments):
        self.deployments = list(deployments)

    def list_deployments(self, namespace):
        return SimpleNamespace(items=list(self.deployments))


def _oracle(deployments):
    kubectl = _KubeCtl(deployments)
    problem = SimpleNamespace(kubectl=kubectl, namespace="hotel-reservation")
    oracle = CpuThrottlingMitigationOracle(problem, "geo")
    oracle.injected_cpu_limit = "125m"
    oracle.rollout_timeout_seconds = 0
    oracle.poll_interval_seconds = 0
    oracle.capture_baseline()
    return oracle, kubectl


@pytest.mark.parametrize(
    ("cpu_limit", "expected"),
    [("255m", True), (None, True), ("250m", False)],
    ids=["raised-limit", "removed-limit", "exactly-twice"],
)
def test_cpu_limit_outcomes(cpu_limit, expected):
    oracle, _ = _oracle([_deployment("geo", cpu_limit=cpu_limit), _deployment("frontend")])

    assert oracle.evaluate()["success"] is expected


@pytest.mark.parametrize(
    "current_deployments",
    [
        [_deployment("geo", cpu_limit="255m")],
        [
            _deployment("geo", cpu_limit="255m"),
            _deployment("frontend", replicas=0, ready=0, updated=0, available=0),
        ],
        [
            _deployment("geo", cpu_limit="255m"),
            _deployment("frontend", replicas=2, ready=1, updated=1, available=1),
        ],
    ],
    ids=["missing", "scaled-to-zero", "partially-available"],
)
def test_unhealthy_required_deployments_are_rejected(current_deployments):
    oracle, kubectl = _oracle([_deployment("geo", cpu_limit="255m"), _deployment("frontend")])
    kubectl.deployments = current_deployments

    assert oracle.evaluate()["success"] is False


def test_missing_baseline_fails_closed():
    oracle, _ = _oracle([_deployment("geo", cpu_limit="255m"), _deployment("frontend")])
    oracle.replica_count = {}

    assert oracle.evaluate()["success"] is False


def test_deployment_api_failure_is_rejected():
    oracle, kubectl = _oracle([_deployment("geo", cpu_limit="255m"), _deployment("frontend")])

    def fail_to_list(_namespace):
        raise RuntimeError("API unavailable")

    kubectl.list_deployments = fail_to_list

    assert oracle.evaluate()["success"] is False
