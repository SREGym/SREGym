from types import SimpleNamespace

import pytest

from sregym.conductor.oracles.unit_mismatch_gomemlimit_mitigation import (
    UnitMismatchGomemlimitMitigation,
    parse_go_memory_limit,
)

BASELINE_IMAGE = "ghcr.io/open-telemetry/demo:2.2.0-checkout"
BASELINE_RESOURCES = {"limits": {"memory": "20Mi"}, "requests": {}}
GOOD = "16MiB"
BAD = "16MB"

# Every case below was verified against ghcr.io/open-telemetry/demo:2.2.0-checkout
# by running the real binary with that GOMEMLIMIT and checking whether the Go
# runtime emitted "fatal error: malformed GOMEMLIMIT" before main().
ACCEPTED_BY_GO = ["16MiB", "16KiB", "16GiB", "16TiB", "16B", "16", "16777216", "0", "9223372036854775807", "off", ""]
REJECTED_BY_GO = ["16MB", "16Mi", "16mib", "OFF", "-1", "16.5MiB", " 16MiB", "16MiB ", "16EiB", "MiB", "abc"]


@pytest.mark.parametrize("value", ACCEPTED_BY_GO)
def test_parser_accepts_what_the_go_runtime_accepts(value):
    assert parse_go_memory_limit(value) is not None


@pytest.mark.parametrize("value", REJECTED_BY_GO)
def test_parser_rejects_what_the_go_runtime_rejects(value):
    assert parse_go_memory_limit(value) is None


def test_parser_returns_byte_values():
    assert parse_go_memory_limit("16MiB") == 16 * 1024**2
    assert parse_go_memory_limit("1KiB") == 1024
    assert parse_go_memory_limit("512B") == 512
    assert parse_go_memory_limit("16777216") == 16777216


def test_unset_and_off_mean_no_limit():
    assert parse_go_memory_limit(None) == parse_go_memory_limit("off")
    assert parse_go_memory_limit(None) > 1024**3


def _container(*, env_value=GOOD, env_set=True, image=BASELINE_IMAGE, resources=None):
    env = []
    if env_set:
        env.append(SimpleNamespace(name="GOMEMLIMIT", value=env_value))
    env.append(SimpleNamespace(name="CHECKOUT_PORT", value="8080"))
    resources = BASELINE_RESOURCES if resources is None else resources
    return SimpleNamespace(
        name="checkout",
        image=image,
        env=env,
        resources=SimpleNamespace(limits=resources["limits"], requests=resources["requests"]),
    )


def _deployment(*, replicas=1, generation=1, observed=1, ready=None, container=None):
    ready = replicas if ready is None else ready
    return SimpleNamespace(
        metadata=SimpleNamespace(name="checkout", generation=generation),
        spec=SimpleNamespace(
            replicas=replicas,
            template=SimpleNamespace(
                spec=SimpleNamespace(
                    containers=[container or _container()],
                    dns_policy="ClusterFirst",
                    dns_config=None,
                    image_pull_secrets=None,
                )
            ),
        ),
        status=SimpleNamespace(
            observed_generation=observed,
            replicas=replicas,
            updated_replicas=replicas,
            ready_replicas=ready,
            available_replicas=ready,
            unavailable_replicas=replicas - ready,
        ),
    )


def _pod(name="checkout-new-abc", rs="checkout-new", ready=True, phase="Running"):
    return SimpleNamespace(
        metadata=SimpleNamespace(
            name=name,
            deletion_timestamp=None,
            owner_references=[SimpleNamespace(kind="ReplicaSet", name=rs)],
        ),
        status=SimpleNamespace(phase=phase, container_statuses=[SimpleNamespace(ready=ready)]),
    )


class _KubeCtl:
    def __init__(self, *, deployment=None, pods=None, replicasets=None):
        self.deployment = deployment or _deployment()
        self.pods = [_pod()] if pods is None else pods
        self.replicasets = (
            replicasets
            if replicasets is not None
            else [SimpleNamespace(metadata=SimpleNamespace(name="checkout-new"), spec=SimpleNamespace(replicas=1))]
        )
        self.core_v1_api = SimpleNamespace()

    def get_deployment(self, name, namespace):
        if self.deployment is None:
            raise RuntimeError(f"deployment {name} not found")
        return self.deployment

    def list_pods(self, namespace):
        return SimpleNamespace(items=self.pods)

    def get_matching_replicasets(self, namespace, deployment_name):
        return self.replicasets


def _oracle(kubectl=None, *, probe_ok=True, baseline=True):
    kubectl = kubectl or _KubeCtl()
    problem = SimpleNamespace(
        namespace="astronomy-shop",
        faulty_service="checkout",
        env_var="GOMEMLIMIT",
        service_port=8080,
        probe={
            "service": "checkout",
            "rpc": "oteldemo.CheckoutService/PlaceOrder",
            "payload": {},
            "success_token": "orderId",
            "proto": None,
            "prerequisite": None,
        },
        kubectl=kubectl,
    )
    oracle = UnitMismatchGomemlimitMitigation(problem)
    if baseline:
        oracle.baseline_replicas = 1
        oracle.baseline_image = BASELINE_IMAGE
        oracle.baseline_resources = BASELINE_RESOURCES
    oracle.rollout_timeout_seconds = 0
    oracle.poll_interval_seconds = 0
    oracle._attempt_service_probe = lambda deployment: probe_ok
    oracle.probe_retry_delay_seconds = 0
    return oracle


def test_accepts_the_original_value_restored():
    assert _oracle().evaluate()["success"] is True


@pytest.mark.parametrize("value", ["32MiB", "1GiB", "16777216", "off"])
def test_accepts_any_valid_value_not_just_the_original(value):
    # The RFC is explicit that the exact original string must not be required.
    kubectl = _KubeCtl(deployment=_deployment(container=_container(env_value=value)))

    assert _oracle(kubectl).evaluate()["success"] is True


def test_accepts_removing_the_override_entirely():
    kubectl = _KubeCtl(deployment=_deployment(container=_container(env_set=False)))

    assert _oracle(kubectl).evaluate()["success"] is True


def test_cheat_leaving_the_invalid_value_in_place():
    kubectl = _KubeCtl(deployment=_deployment(container=_container(env_value=BAD)))

    result = _oracle(kubectl).evaluate()

    assert result["success"] is False
    assert "Go runtime rejects" in result["reason"]


@pytest.mark.parametrize("value", ["0", "16B", "1B"])
def test_cheat_syntactically_valid_but_useless_value(value):
    kubectl = _KubeCtl(deployment=_deployment(container=_container(env_value=value)))

    result = _oracle(kubectl).evaluate()

    assert result["success"] is False
    assert "too small" in result["reason"]


def test_cheat_scale_to_zero():
    kubectl = _KubeCtl(deployment=_deployment(replicas=0, ready=0))

    result = _oracle(kubectl).evaluate()

    assert result["success"] is False
    assert "scaled to 0" in result["reason"]


def test_cheat_scale_below_baseline():
    kubectl = _KubeCtl(deployment=_deployment(replicas=1))
    oracle = _oracle(kubectl)
    oracle.baseline_replicas = 3

    result = oracle.evaluate()

    assert result["success"] is False
    assert "below its pre-fault replica count" in result["reason"]


def test_cheat_swap_the_image():
    kubectl = _KubeCtl(deployment=_deployment(container=_container(image="busybox:1.36")))

    result = _oracle(kubectl).evaluate()

    assert result["success"] is False
    assert "image was changed" in result["reason"]


def test_cheat_raise_the_kubernetes_memory_limit():
    kubectl = _KubeCtl(
        deployment=_deployment(
            container=_container(env_value=BAD, resources={"limits": {"memory": "512Mi"}, "requests": {}})
        )
    )

    result = _oracle(kubectl).evaluate()

    assert result["success"] is False
    assert "resource requests or limits were changed" in result["reason"]


def test_cheat_old_replicaset_pod_still_serving():
    # The surviving pod belongs to a ReplicaSet that has been scaled to zero,
    # so it still carries the pre-fault environment and must not count.
    kubectl = _KubeCtl(
        pods=[_pod(name="checkout-old-xyz", rs="checkout-old")],
        replicasets=[
            SimpleNamespace(metadata=SimpleNamespace(name="checkout-old"), spec=SimpleNamespace(replicas=0)),
            SimpleNamespace(metadata=SimpleNamespace(name="checkout-new"), spec=SimpleNamespace(replicas=1)),
        ],
    )

    result = _oracle(kubectl).evaluate()

    assert result["success"] is False
    assert "active ReplicaSet" in result["reason"]


def test_cheat_readiness_theater_pod_running_but_rpc_fails():
    result = _oracle(probe_ok=False).evaluate()

    assert result["success"] is False
    assert "did not succeed" in result["reason"]


def test_rejects_incomplete_rollout():
    kubectl = _KubeCtl(deployment=_deployment(generation=2, observed=1, ready=0))

    result = _oracle(kubectl).evaluate()

    assert result["success"] is False
    assert "did not complete its current rollout" in result["reason"]


def test_rejects_deleted_deployment():
    kubectl = _KubeCtl()
    kubectl.deployment = None

    result = _oracle(kubectl).evaluate()

    assert result["success"] is False
    assert "does not exist" in result["reason"]


def test_requires_a_captured_baseline():
    result = _oracle(baseline=False).evaluate()

    assert result["success"] is False
    assert "baseline" in result["reason"]


def test_probe_retries_before_declaring_failure():
    # A real order crosses seven services; one slow call must not fail a
    # correct repair. Regression test for a live Stratus run where the agent
    # fixed the value correctly and a single 25s attempt timed out.
    oracle = _oracle()
    calls = {"n": 0}

    def flaky(deployment):
        calls["n"] += 1
        return calls["n"] >= 3

    oracle._attempt_service_probe = flaky

    assert oracle.evaluate()["success"] is True
    assert calls["n"] == 3


def test_probe_gives_up_after_the_configured_attempts():
    oracle = _oracle()
    calls = {"n": 0}

    def always_fail(deployment):
        calls["n"] += 1
        return False

    oracle._attempt_service_probe = always_fail
    result = oracle.evaluate()

    assert result["success"] is False
    assert calls["n"] == oracle.probe_attempts
