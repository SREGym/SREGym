"""IncorrectImageMitigationOracle must see the injected image however it is
spelled, and must not accept a replacement image that never rolls out."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from kubernetes.client.rest import ApiException

from sregym.conductor.oracles.incorrect_image_mitigation import (
    IncorrectImageMitigationOracle,
    ProductCatalogImageMitigationOracle,
)

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
        metadata=SimpleNamespace(name="product-catalog", uid="deployment-1", generation=1, deletion_timestamp=None),
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
    status = SimpleNamespace(name="app", restart_count=restarts)
    return [
        SimpleNamespace(
            metadata=SimpleNamespace(uid="pod-1", deletion_timestamp=None),
            status=SimpleNamespace(container_statuses=[status]),
        )
    ]


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


@pytest.fixture
def clock(monkeypatch):
    class Clock:
        now = 0

        def monotonic(self):
            return self.now

        def sleep(self, duration):
            self.now += duration

    clock = Clock()
    monkeypatch.setattr("sregym.conductor.oracles.incorrect_image_mitigation.time.monotonic", clock.monotonic)
    monkeypatch.setattr("sregym.conductor.oracles.incorrect_image_mitigation.time.sleep", clock.sleep)
    monkeypatch.setattr(IncorrectImageMitigationOracle, "rollout_timeout_seconds", 120)
    monkeypatch.setattr(IncorrectImageMitigationOracle, "stability_seconds", 30)
    monkeypatch.setattr(IncorrectImageMitigationOracle, "poll_interval_seconds", 2)
    return clock


def _multiple_targets(clock, ready_after):
    def current(name, namespace):
        deployment = _deployment(GOOD, ready=int(clock.now >= ready_after))
        deployment.metadata.name = name
        return deployment

    oracle = _oracle(_deployment(GOOD))
    names = [f"mongodb-{number}" for number in range(6)]
    oracle.problem.faulty_service = names
    oracle.actual_images = dict.fromkeys(names, BAD)
    oracle.problem.kubectl.get_deployment = current
    return oracle


@pytest.mark.parametrize(("ready_after", "elapsed"), [(0, 30), (115, 146), (120, 150)])
def test_targets_share_rollout_and_stability_deadlines(clock, ready_after, elapsed):
    oracle = _multiple_targets(clock, ready_after)
    assert oracle.evaluate() == {"success": True}
    assert clock.now == elapsed


def test_rollout_timeout_is_not_multiplied_by_target_count(clock):
    assert not _multiple_targets(clock, ready_after=121).evaluate()["success"]
    assert clock.now == 120


def test_earlier_target_is_rechecked_while_later_targets_stabilize(clock):
    oracle = _multiple_targets(clock, ready_after=20)
    read = oracle.problem.kubectl.get_deployment

    def current(name, namespace):
        deployment = read(name, namespace)
        if name == "mongodb-0" and clock.now >= 45:
            deployment.status.ready_replicas = 0
        return deployment

    oracle.problem.kubectl.get_deployment = current
    assert not oracle.evaluate()["success"]
    assert clock.now == 46


@pytest.mark.parametrize("change", ["image", "generation", "pod", "restart"])
def test_changes_during_stability_are_not_hidden(clock, change):
    oracle = _oracle(_deployment(GOOD))

    def current(name, namespace):
        deployment = _deployment(BAD if change == "image" and clock.now >= 10 else GOOD)
        if change == "generation" and clock.now >= 10:
            deployment.metadata.generation = deployment.status.observed_generation = 2
        return deployment

    def pods(deployment, namespace):
        pods = _pods(int(change == "restart" and clock.now >= 10))
        if change == "pod" and clock.now >= 10:
            pods[0].metadata.uid = "replacement-pod"
        return pods

    oracle.problem.kubectl.get_deployment = current
    oracle.problem.kubectl.get_deployment_pods = pods
    result = oracle.evaluate()
    assert result["success"] is False
    assert clock.now == 10


def test_no_owned_running_containers_cannot_pass():
    oracle = _oracle(_deployment(GOOD))
    oracle.problem.kubectl.get_deployment_pods = lambda *_: []
    assert oracle.evaluate()["reason"] == "no_matching_pods"


PRODUCTS = '[{"id":"OLJCESPC7Z","name":"Telescope","priceUsd":{"units":100}}]'


def _catalog_oracle():
    basic = _oracle(_deployment(GOOD))
    basic.problem.app = SimpleNamespace(frontend_service="frontend-proxy")
    api = Mock()
    basic.problem.kubectl.core_v1_api = api
    api.read_namespaced_service.return_value = SimpleNamespace(spec=SimpleNamespace(ports=[SimpleNamespace(port=8080)]))
    api.read_namespaced_pod.return_value = SimpleNamespace(status=SimpleNamespace(phase="Succeeded"))
    api.read_namespaced_pod_log.return_value = "\n".join([PRODUCTS] * 3)
    return ProductCatalogImageMitigationOracle(basic.problem, basic.actual_images), api


def test_catalog_recovery_requires_real_product_data():
    oracle, api = _catalog_oracle()
    assert oracle.evaluate() == {"success": True}
    pod = api.create_namespaced_pod.call_args.args[1]
    assert pod.spec.automount_service_account_token is False
    assert pod.spec.active_deadline_seconds == 60
    assert "frontend-proxy.astronomy-shop.svc.cluster.local:8080/api/products" in pod.spec.containers[0].command[2]
    api.delete_namespaced_pod.assert_called_once()


@pytest.mark.parametrize("body", ["OK\nOK\nOK", "[]\n[]\n[]", PRODUCTS, '[{"id":"unrelated"}]\n' * 3])
def test_http_success_without_three_catalog_responses_fails(body):
    oracle, api = _catalog_oracle()
    api.read_namespaced_pod_log.return_value = body
    assert oracle.evaluate()["reason"] == "connectivity_probe_failed"
    api.delete_namespaced_pod.assert_called_once()


def test_multiline_product_json_is_accepted():
    oracle, _ = _catalog_oracle()
    assert oracle._valid_products((PRODUCTS.replace(",", ",\n") + "\n") * 3)


def test_failed_http_probe_is_rejected_and_cleaned_up():
    oracle, api = _catalog_oracle()
    api.read_namespaced_pod.return_value.status.phase = "Failed"
    assert oracle.evaluate()["reason"] == "connectivity_probe_failed"
    api.read_namespaced_pod_log.assert_not_called()
    api.delete_namespaced_pod.assert_called_once()


def test_catalog_probe_has_a_bounded_wait(clock):
    oracle, api = _catalog_oracle()
    api.read_namespaced_pod.return_value.status.phase = "Pending"
    assert oracle.evaluate()["reason"] == "connectivity_probe_failed"
    assert clock.now == 90  # one shared stability window plus the probe deadline
    api.delete_namespaced_pod.assert_called_once()


def test_catalog_api_error_is_not_reported_as_bad_products():
    oracle, api = _catalog_oracle()
    api.read_namespaced_pod.side_effect = ApiException(status=503)
    with pytest.raises(ApiException):
        oracle.evaluate()
    api.delete_namespaced_pod.assert_called_once()


def test_failed_rollout_does_not_run_catalog_probe():
    oracle, api = _catalog_oracle()
    oracle.problem.kubectl.get_deployment = lambda *_: _deployment(BAD)
    assert oracle.evaluate()["reason"] == "fault_still_present"
    api.create_namespaced_pod.assert_not_called()


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
