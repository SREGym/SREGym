from types import SimpleNamespace

from sregym.conductor.oracles.priority_preemption_mitigation import PriorityPreemptionMitigationOracle


def _oracle():
    oracle = object.__new__(PriorityPreemptionMitigationOracle)
    oracle.problem = SimpleNamespace(
        namespace="hotel-reservation",
        faulty_service="reservation",
        PRESSURE_NAMESPACE="analytics-batch",
        PRESSURE_DEPLOYMENT="tenant-ingester",
        PLATFORM_PRIORITY_CLASS="platform-medium",
        PRODUCTION_PRIORITY_CLASS="production-critical",
        target_request_memory="512Mi",
        pressure_request_memory="2Gi",
    )
    return oracle


def _deployment(name, replicas=1, ready=1, priority_class=None, memory="512Mi"):
    return SimpleNamespace(
        metadata=SimpleNamespace(name=name),
        spec=SimpleNamespace(
            replicas=replicas,
            template=SimpleNamespace(
                spec=SimpleNamespace(
                    priority_class_name=priority_class,
                    containers=[
                        SimpleNamespace(
                            resources=SimpleNamespace(
                                requests={"memory": memory},
                            )
                        )
                    ],
                )
            ),
        ),
        status=SimpleNamespace(ready_replicas=ready),
    )


def _priority_class(name, value, global_default=False):
    return SimpleNamespace(
        metadata=SimpleNamespace(name=name),
        value=value,
        global_default=global_default,
    )


def test_all_deployments_ready_rejects_scaled_down_shortcut():
    oracle = _oracle()
    oracle.apps_v1 = SimpleNamespace(
        list_namespaced_deployment=lambda namespace: SimpleNamespace(
            items=[
                _deployment("reservation"),
                _deployment("frontend", replicas=0, ready=0),
            ]
        )
    )

    assert oracle._all_deployments_ready("hotel-reservation") is False


def test_all_deployments_ready_requires_ready_replicas():
    oracle = _oracle()
    oracle.apps_v1 = SimpleNamespace(
        list_namespaced_deployment=lambda namespace: SimpleNamespace(
            items=[
                _deployment("reservation", replicas=1, ready=1),
                _deployment("frontend", replicas=1, ready=0),
            ]
        )
    )

    assert oracle._all_deployments_ready("hotel-reservation") is False


def test_request_not_reduced_rejects_target_memory_cut():
    oracle = _oracle()
    deployment = _deployment("reservation", memory="128Mi")

    assert oracle._request_not_reduced(deployment, "512Mi") is False


def test_request_not_reduced_accepts_equal_or_larger_request():
    oracle = _oracle()
    deployment = _deployment("tenant-ingester", memory="2Gi")

    assert oracle._request_not_reduced(deployment, "2Gi") is True


def test_request_check_is_disabled_when_injection_did_not_record_expected_memory():
    oracle = _oracle()
    deployment = _deployment("reservation", memory="128Mi")

    assert oracle._request_not_reduced(deployment, None) is True


def test_target_priority_accepts_custom_class_above_platform():
    oracle = _oracle()
    deployment = _deployment("reservation", priority_class="reservation-high-priority")
    platform = _priority_class("platform-medium", 100000)
    classes = {
        "reservation-high-priority": _priority_class("reservation-high-priority", 200000),
    }
    oracle._read_priority_class = classes.get

    assert oracle._target_priority_is_safe(deployment, platform) is True


def test_target_priority_rejects_missing_or_low_priority_class():
    oracle = _oracle()
    platform = _priority_class("platform-medium", 100000)
    classes = {
        "platform-medium": platform,
        "reservation-low": _priority_class("reservation-low", 50000),
    }
    oracle._read_priority_class = classes.get

    assert oracle._target_priority_is_safe(_deployment("reservation"), platform) is False
    assert (
        oracle._target_priority_is_safe(_deployment("reservation", priority_class="missing-priority"), platform)
        is False
    )
    assert (
        oracle._target_priority_is_safe(_deployment("reservation", priority_class="reservation-low"), platform) is False
    )
