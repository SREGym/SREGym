"""IncorrectImageMitigationOracle must see the injected image however it is
spelled, and must not accept a replacement image that never rolls out."""

from types import SimpleNamespace

import pytest

from sregym.conductor.oracles.incorrect_image_mitigation import IncorrectImageMitigationOracle

BAD = "app-image:latest"
GOOD = "ghcr.io/open-telemetry/demo:2.0.2-product-catalog"


@pytest.fixture(autouse=True)
def _fast_rollout_wait(monkeypatch):
    monkeypatch.setattr(IncorrectImageMitigationOracle, "rollout_timeout_seconds", 0.05, raising=False)
    monkeypatch.setattr(IncorrectImageMitigationOracle, "poll_interval_seconds", 0.01, raising=False)
    monkeypatch.setattr(IncorrectImageMitigationOracle, "stability_seconds", 0.05, raising=False)


def _deployment(image, *, replicas=1, ready=None):
    ready = replicas if ready is None else ready
    return SimpleNamespace(
        metadata=SimpleNamespace(name="product-catalog", generation=1, deletion_timestamp=None),
        spec=SimpleNamespace(
            replicas=replicas,
            template=SimpleNamespace(spec=SimpleNamespace(containers=[SimpleNamespace(image=image)])),
        ),
        status=SimpleNamespace(
            observed_generation=1,
            replicas=replicas,
            updated_replicas=replicas,
            ready_replicas=ready,
            available_replicas=ready,
            unavailable_replicas=replicas - ready,
        ),
    )


def _pods(restarts):
    status = SimpleNamespace(restart_count=restarts)
    return [SimpleNamespace(status=SimpleNamespace(container_statuses=[status]))]


def _oracle(deployment, restarts=None):
    restarts = iter(restarts or [0] * 1000)
    kubectl = SimpleNamespace(
        get_deployment=lambda name, namespace: deployment,
        get_deployment_pods=lambda deployment, namespace: _pods(next(restarts)),
    )
    problem = SimpleNamespace(kubectl=kubectl, namespace="astronomy-shop", faulty_service=["product-catalog"])
    return IncorrectImageMitigationOracle(problem, actual_images={"product-catalog": BAD})


def test_injected_image_still_set_fails():
    verdict = _oracle(_deployment(BAD, ready=0)).evaluate()
    assert verdict["success"] is False
    assert verdict["reason"] == "fault_still_present"


def test_restored_image_that_rolls_out_passes():
    assert _oracle(_deployment(GOOD)).evaluate() == {"success": True}


def test_injected_image_spelled_in_full_still_fails():
    """docker.io/library/app-image:latest is the image the injector wrote."""
    verdict = _oracle(_deployment("docker.io/library/app-image:latest", ready=0)).evaluate()
    assert verdict["reason"] == "fault_still_present"


def test_another_image_that_never_becomes_ready_fails():
    """A second non-existent tag stays in ImagePullBackOff: no fix."""
    verdict = _oracle(_deployment("app-image:v2", ready=0)).evaluate()
    assert verdict["success"] is False
    assert verdict["reason"] == "required_deployment_not_rolled_out"


def test_new_image_with_zero_replicas_fails():
    verdict = _oracle(_deployment(GOOD, replicas=0, ready=0)).evaluate()
    assert verdict["success"] is False
    assert verdict["reason"] == "required_deployment_not_rolled_out"


def test_new_image_that_crash_loops_fails():
    """Without a readiness probe a crashing container is Ready between restarts."""
    verdict = _oracle(_deployment(GOOD), restarts=range(1000)).evaluate()
    assert verdict["success"] is False
    assert verdict["reason"] == "required_deployment_not_rolled_out"


@pytest.mark.parametrize(
    ("first", "second", "same"),
    [
        ("app-image:latest", "docker.io/library/app-image:latest", True),
        ("app-image", "app-image:latest", True),
        ("mongo:8.0.14-rc0", "index.docker.io/library/mongo:8.0.14-rc0", True),
        ("ghcr.io/sregym/hotel:20260912.1@sha256:8bc5", "ghcr.io/sregym/hotel@sha256:8bc5", True),
        ("localhost:5000/app:1", "localhost:5000/app:2", False),
        ("ghcr.io/sregym/hotel:20260912.1", "ghcr.io/sregym/hotel@sha256:8bc5", False),
    ],
)
def test_normalize_image(first, second, same):
    from sregym.conductor.oracles.incorrect_image_mitigation import normalize_image

    assert (normalize_image(first) == normalize_image(second)) is same
