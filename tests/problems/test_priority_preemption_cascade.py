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


def test_target_request_is_capped_on_large_nodes():
    problem = _problem()
    problem.kubectl = SimpleNamespace(format_k8s_memory=lambda kib: f"{kib}Ki")
    problem._node_allocatable_memory_kib = lambda node: 192 * 1024 * 1024
    problem._node_requested_memory_kib = lambda node: 0

    target = problem._target_request_for_node("worker-a")

    assert target == f"{problem.TARGET_REQUEST_CAP_KIB}Ki"


def test_capacity_padding_uses_believable_chunk_sizes_on_large_nodes():
    problem = _problem()
    free_kib = 184 * 1024 * 1024
    desired_free_kib = 4 * 1024 * 1024

    requests = problem._padding_request_sizes_kib(free_kib, desired_free_kib)
    remaining_free_kib = free_kib - sum(requests)

    assert requests
    assert set(requests) <= {problem.PADDING_REQUEST_KIB, problem.MIN_PADDING_REQUEST_KIB}
    assert desired_free_kib <= remaining_free_kib < desired_free_kib + problem.MIN_PADDING_REQUEST_KIB


def test_capacity_padding_skips_when_node_is_already_near_desired_free():
    problem = _problem()
    desired_free_kib = 4 * 1024 * 1024
    free_kib = desired_free_kib + problem.MIN_PADDING_REQUEST_KIB - 1

    assert problem._padding_request_sizes_kib(free_kib, desired_free_kib) == []


def test_capacity_padding_uses_minimum_chunk_for_partial_reserve():
    problem = _problem()
    desired_free_kib = 4 * 1024 * 1024
    free_kib = desired_free_kib + problem.MIN_PADDING_REQUEST_KIB

    assert problem._padding_request_sizes_kib(free_kib, desired_free_kib) == [problem.MIN_PADDING_REQUEST_KIB]


def test_padding_request_sizes_for_node_uses_current_requested_memory():
    problem = _problem()
    allocatable_kib = 32 * 1024 * 1024
    requested_kib = 8 * 1024 * 1024
    problem._node_allocatable_memory_kib = lambda node: allocatable_kib
    problem._node_requested_memory_kib = lambda node: requested_kib

    requests = problem._padding_request_sizes_for_node("worker-a")

    assert requests == problem._padding_request_sizes_kib(allocatable_kib - requested_kib)


def test_large_node_padding_keeps_pressure_request_realistic_and_preempting():
    problem = _problem()
    problem.kubectl = SimpleNamespace(format_k8s_memory=lambda kib: f"{kib}Ki")
    allocatable_kib = 192 * 1024 * 1024
    target_request_kib = problem.TARGET_REQUEST_CAP_KIB
    free_before_padding_kib = allocatable_kib - target_request_kib
    padding_requests = problem._padding_request_sizes_kib(free_before_padding_kib)
    free_after_padding_kib = free_before_padding_kib - sum(padding_requests)
    problem._node_allocatable_memory_kib = lambda node: allocatable_kib
    problem._node_requested_memory_kib = lambda node: allocatable_kib - free_after_padding_kib
    target = _pod("reservation-0", memory=f"{target_request_kib}Ki")

    pressure = problem._pressure_request_for_target_pod(target)
    pressure_kib = int(pressure.removesuffix("Ki"))
    headroom_kib = min(problem.SCHEDULING_HEADROOM_KIB, target_request_kib // 4)

    assert pressure_kib > free_after_padding_kib
    assert pressure_kib <= free_after_padding_kib + target_request_kib - headroom_kib
    assert pressure_kib <= problem.PADDING_REQUEST_KIB


def test_create_capacity_padding_uses_platform_priority_and_target_node():
    problem = _problem()
    problem.kubectl = SimpleNamespace(format_k8s_memory=lambda kib: f"{kib}Ki")
    problem._padding_request_sizes_for_node = lambda node: [
        problem.PADDING_REQUEST_KIB,
        problem.MIN_PADDING_REQUEST_KIB,
    ]
    namespaces = []
    created = []
    ready = []
    problem._ensure_namespace = namespaces.append
    problem._wait_for_deployment_ready = lambda name, namespace: ready.append((name, namespace))
    problem.apps_v1 = SimpleNamespace(
        create_namespaced_deployment=lambda namespace, body: created.append((namespace, body))
    )

    names = problem._create_capacity_padding()

    assert namespaces == [problem.PRESSURE_NAMESPACE]
    assert names == ["report-cache-shard-0", "report-cache-shard-1"]
    assert ready == [(name, problem.PRESSURE_NAMESPACE) for name in names]
    first_spec = created[0][1]["spec"]["template"]["spec"]
    assert first_spec["priorityClassName"] == problem.PLATFORM_PRIORITY_CLASS
    assert first_spec["nodeSelector"] == {"kubernetes.io/hostname": problem.target_node}
    assert first_spec["containers"][0]["resources"]["requests"] == {
        "cpu": "10m",
        "memory": f"{problem.PADDING_REQUEST_KIB}Ki",
    }


def test_create_padding_deployment_replaces_existing_deployment_on_conflict():
    problem = _problem()
    replaced = []

    class _Apps:
        def create_namespaced_deployment(self, namespace, body):
            raise _api_exception(409)

        def replace_namespaced_deployment(self, name, namespace, body):
            replaced.append((name, namespace, body))

    problem.apps_v1 = _Apps()

    name = problem._create_padding_deployment(0, "16Gi")

    assert name == "report-cache-shard-0"
    assert replaced[0][0:2] == (name, problem.PRESSURE_NAMESPACE)
    assert replaced[0][2]["spec"]["template"]["spec"]["priorityClassName"] == problem.PLATFORM_PRIORITY_CLASS


def test_create_capacity_padding_skips_namespace_when_no_padding_needed():
    problem = _problem()
    problem._padding_request_sizes_for_node = lambda node: []
    problem._ensure_namespace = lambda name: pytest.fail("namespace should not be created")

    assert problem._create_capacity_padding() == []


def test_create_capacity_padding_cleans_namespace_when_padding_readiness_fails():
    problem = _problem()
    problem.kubectl = SimpleNamespace(format_k8s_memory=lambda kib: f"{kib}Ki")
    problem._padding_request_sizes_for_node = lambda node: [problem.PADDING_REQUEST_KIB]
    created = []
    deleted = []
    problem._ensure_namespace = lambda name: None
    problem._delete_pressure_namespace = lambda: deleted.append(problem.PRESSURE_NAMESPACE)
    problem.apps_v1 = SimpleNamespace(
        create_namespaced_deployment=lambda namespace, body: created.append((namespace, body))
    )

    def fail_ready(name, namespace):
        raise TimeoutError("padding pending")

    problem._wait_for_deployment_ready = fail_ready

    with pytest.raises(TimeoutError, match="padding pending"):
        problem._create_capacity_padding()

    assert created
    assert deleted == [problem.PRESSURE_NAMESPACE]


def test_pressure_and_padding_use_same_platform_priority_class():
    problem = _problem()
    problem.pressure_request_memory = "12Gi"
    problem._ensure_namespace = lambda name: None
    created = []
    problem.apps_v1 = SimpleNamespace(
        create_namespaced_deployment=lambda namespace, body: created.append((namespace, body))
    )

    problem._create_pressure_deployment()
    problem._create_padding_deployment(0, "16Gi")

    pressure_spec = created[0][1]["spec"]["template"]["spec"]
    padding_spec = created[1][1]["spec"]["template"]["spec"]
    assert pressure_spec["priorityClassName"] == problem.PLATFORM_PRIORITY_CLASS
    assert padding_spec["priorityClassName"] == problem.PLATFORM_PRIORITY_CLASS


def test_inject_sizes_pressure_after_padding_and_cleans_namespace_on_setup_failure():
    problem = _problem()
    target = _pod("reservation-0", memory="8Gi")
    order = []
    deleted = []
    problem._delete_support_resources = lambda: order.append("delete-support")
    problem._capture_priority_classes = lambda: None
    problem._target_pod = lambda: target
    problem._target_request_for_node = lambda node: "8Gi"
    problem._capture_app_template_state = lambda: None
    problem._patch_target_requests = lambda: None
    problem._pin_target_to_node = lambda: None
    problem._ensure_target_preemptable = lambda pod: None
    problem._create_or_replace_priority_class = lambda *args, **kwargs: None
    problem._protect_peer_deployments = lambda: None
    problem._create_capacity_padding = lambda: order.append("padding") or ["report-cache-shard-0"]
    problem._delete_pressure_namespace = lambda: deleted.append(problem.PRESSURE_NAMESPACE)

    def fail_pressure_sizing(pod):
        order.append("pressure-size")
        raise RuntimeError("pressure sizing failed")

    problem._pressure_request_for_target_pod = fail_pressure_sizing

    with pytest.raises(RuntimeError, match="pressure sizing failed"):
        problem.inject_fault()

    assert order.index("padding") < order.index("pressure-size")
    assert deleted == [problem.PRESSURE_NAMESPACE]


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
