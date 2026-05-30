from types import SimpleNamespace

import pytest

from sregym.conductor.problems.priority_preemption_cascade import PriorityPreemptionCascadeHotelReservation


def _problem():
    problem = object.__new__(PriorityPreemptionCascadeHotelReservation)
    problem.namespace = "hotel-reservation"
    problem.faulty_service = "reservation"
    problem.target_node = "worker-a"
    problem._priority_class_snapshots = {}
    problem._deployment_priority_classes = {}
    problem._target_original_resources = None
    problem._target_original_node_selector = None
    return problem


def _deployment(name, replicas=1, ready=1, priority_class=None, memory="512Mi", node_selector=None):
    return SimpleNamespace(
        metadata=SimpleNamespace(name=name),
        spec=SimpleNamespace(
            replicas=replicas,
            selector=SimpleNamespace(match_labels={"app": name}),
            template=SimpleNamespace(
                spec=SimpleNamespace(
                    priority_class_name=priority_class,
                    node_selector=node_selector or {},
                    containers=[
                        SimpleNamespace(
                            name=name,
                            resources=SimpleNamespace(
                                requests={"memory": memory},
                                limits={},
                            ),
                        )
                    ],
                )
            ),
        ),
        status=SimpleNamespace(
            ready_replicas=ready,
            updated_replicas=ready,
            available_replicas=ready,
            unavailable_replicas=max(0, replicas - ready),
            observed_generation=1,
        ),
    )


def _pod(name, phase="Running", node="worker-a", priority=0, priority_class=None, memory="512Mi"):
    return SimpleNamespace(
        metadata=SimpleNamespace(name=name),
        spec=SimpleNamespace(
            node_name=node,
            priority=priority,
            priority_class_name=priority_class,
            containers=[
                SimpleNamespace(
                    resources=SimpleNamespace(
                        requests={"memory": memory},
                    )
                )
            ],
        ),
        status=SimpleNamespace(phase=phase),
    )


def test_pressure_request_satisfies_dynamic_preemption_inequalities():
    problem = _problem()
    problem.kubectl = SimpleNamespace(format_k8s_memory=lambda kib: f"{kib}Ki")
    problem._node_allocatable_memory_kib = lambda node: 4 * 1024 * 1024
    problem._node_requested_memory_kib = lambda node: 1 * 1024 * 1024
    target = _pod("reservation-0", memory="512Mi")

    pressure = problem._pressure_request_for_target_pod(target)
    pressure_kib = int(pressure.removesuffix("Ki"))
    free_kib = 3 * 1024 * 1024
    target_kib = 512 * 1024
    headroom_kib = min(problem.SCHEDULING_HEADROOM_KIB, target_kib // 4)

    assert pressure_kib > free_kib
    assert pressure_kib <= free_kib + target_kib - headroom_kib
    assert pressure_kib + target_kib > free_kib + target_kib


def test_preemption_evidence_requires_scheduler_event_and_replacement_priority():
    problem = _problem()
    target = _deployment("reservation", replicas=1, ready=0)
    pressure = _deployment("tenant-ingester", replicas=1, ready=1)
    problem._preemption_event_seen = lambda: True
    problem._replacement_target_has_platform_priority = lambda: True

    assert problem._preemption_evidence_ready(target, pressure) is True

    problem._preemption_event_seen = lambda: False
    assert problem._preemption_evidence_ready(target, pressure) is False


def test_pressure_pod_must_have_higher_priority_than_target():
    problem = _problem()
    target = _pod("reservation-0", priority=0)
    pressure = _pod(
        "tenant-ingester-0",
        priority=100000,
        priority_class=problem.PLATFORM_PRIORITY_CLASS,
    )

    problem._ensure_pressure_can_preempt_target(pressure, target)

    pressure.spec.priority = 0
    with pytest.raises(RuntimeError, match="is not higher"):
        problem._ensure_pressure_can_preempt_target(pressure, target)


def test_pin_target_to_node_preserves_existing_node_selector_terms():
    problem = _problem()
    problem.target_node = "worker-b"
    patched = []
    problem.apps_v1 = SimpleNamespace(
        read_namespaced_deployment=lambda name, namespace: _deployment(
            "reservation",
            node_selector={"topology.kubernetes.io/zone": "zone-a"},
        ),
        patch_namespaced_deployment=lambda name, namespace, body: patched.append(body),
    )
    problem._wait_for_deployment_ready = lambda name, namespace: None

    problem._pin_target_to_node()

    selector = patched[0]["spec"]["template"]["spec"]["nodeSelector"]
    assert selector == {
        "topology.kubernetes.io/zone": "zone-a",
        "kubernetes.io/hostname": "worker-b",
    }


def test_restore_app_template_state_restores_target_node_selector_and_resources():
    problem = _problem()
    problem._deployment_priority_classes = {"reservation": None}
    problem._target_original_node_selector = {"topology.kubernetes.io/zone": "zone-a"}
    problem._target_original_resources = {"requests": {"memory": "64Mi"}, "limits": {}}
    patched = []
    problem._app_deployments = lambda: [
        _deployment(
            "reservation",
            priority_class=problem.PRODUCTION_PRIORITY_CLASS,
            node_selector={"kubernetes.io/hostname": "worker-b"},
            memory="2Gi",
        )
    ]
    problem.apps_v1 = SimpleNamespace(patch_namespaced_deployment=lambda name, namespace, body: patched.append(body))

    problem._restore_app_template_state()

    pod_spec = patched[0]["spec"]["template"]["spec"]
    assert pod_spec["priorityClassName"] is None
    assert pod_spec["nodeSelector"] == {"topology.kubernetes.io/zone": "zone-a"}
    assert pod_spec["containers"][0]["resources"] == {"requests": {"memory": "64Mi"}}


def test_cleanup_does_not_delete_unlabeled_preexisting_priority_class_without_snapshot():
    problem = _problem()
    deleted = []
    problem._priority_class_has_problem_label = lambda name: False
    problem._delete_priority_class = deleted.append

    problem._restore_or_delete_priority_class(problem.PLATFORM_PRIORITY_CLASS)

    assert deleted == []


def test_cleanup_deletes_priority_class_created_by_problem():
    problem = _problem()
    deleted = []
    problem._priority_class_snapshots = {problem.PLATFORM_PRIORITY_CLASS: None}
    problem._priority_class_has_problem_label = lambda name: False
    problem._delete_priority_class = deleted.append

    problem._restore_or_delete_priority_class(problem.PLATFORM_PRIORITY_CLASS)

    assert deleted == [problem.PLATFORM_PRIORITY_CLASS]


def test_cleanup_removes_priority_references_before_deleting_priority_classes():
    problem = _problem()
    order = []
    problem._restore_app_template_state = lambda: order.append("restore-templates")
    problem._clear_app_priority_references = lambda: order.append("clear-references")
    problem._wait_for_priority_references_removed = lambda: order.append("wait-references")
    problem._delete_pressure_namespace = lambda: order.append("delete-pressure")
    problem._restore_or_delete_priority_class = lambda name: order.append(f"priorityclass:{name}")

    problem._delete_support_resources()

    assert order[:3] == ["restore-templates", "clear-references", "wait-references"]
    assert order[-2:] == [
        f"priorityclass:{problem.PLATFORM_PRIORITY_CLASS}",
        f"priorityclass:{problem.PRODUCTION_PRIORITY_CLASS}",
    ]


def test_wait_for_preemption_error_includes_cluster_event_evidence_when_missing():
    problem = _problem()
    problem.kubectl = SimpleNamespace(exec_command=lambda command: "no preemption event")
    problem.apps_v1 = SimpleNamespace(
        read_namespaced_deployment=lambda name, namespace: _deployment(name, replicas=1, ready=1)
    )
    problem._preemption_evidence_ready = lambda target, pressure: False

    with pytest.raises(TimeoutError, match="scheduler preemption event"):
        problem._wait_for_preemption(timeout=0)


def test_delete_pressure_namespace_tolerates_missing_namespace():
    problem = _problem()

    class _Core:
        def delete_namespace(self, name):
            raise _api_exception(404)

    problem.core_v1 = _Core()

    assert problem._delete_pressure_namespace() is None


def _api_exception(status):
    from kubernetes.client.exceptions import ApiException

    error = ApiException(status=status)
    return error
