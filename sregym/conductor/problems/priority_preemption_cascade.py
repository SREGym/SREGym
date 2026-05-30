"""Problem: PriorityClass cascade preemption disrupts Hotel Reservation.

This models a production scheduler-policy failure where a platform team makes
an intermediate PriorityClass the global default. Existing production pods have
no priority, while a new tenant workload receives the medium default and can
preempt them under resource pressure. Replacement production pods inherit the
same unsafe default, so they cannot preempt the tenant workload back and the
service remains unavailable.

The real-world anchor is Grafana Labs' Hosted Prometheus outage caused by
Kubernetes Pod Priorities. A new Cortex cluster used medium-priority ingesters
while existing production ingesters had no priority, so the new pods preempted
production pods and cascaded through the cluster.
"""

import contextlib
import time

from kubernetes import client
from kubernetes.client.exceptions import ApiException
from kubernetes.utils.quantity import parse_quantity

from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.oracles.priority_preemption_mitigation import PriorityPreemptionMitigationOracle
from sregym.conductor.problems.base import Problem
from sregym.service.apps.hotel_reservation import HotelReservation
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected


class PriorityPreemptionCascadeHotelReservation(Problem):
    """Inject an unsafe global PriorityClass plus a tenant pressure workload."""

    PLATFORM_PRIORITY_CLASS = "platform-medium"
    PRODUCTION_PRIORITY_CLASS = "production-critical"
    PRESSURE_NAMESPACE = "analytics-batch"
    PRESSURE_DEPLOYMENT = "tenant-ingester"
    PRESSURE_LABEL = "tenant-ingester"

    TARGET_REQUEST_RATIO = 0.30
    PRESSURE_PREEMPTION_RATIO = 0.50
    MIN_TARGET_REQUEST_KIB = 256 * 1024
    MIN_PRESSURE_REQUEST_KIB = 512 * 1024
    MIN_PREEMPTION_GAP_KIB = 64 * 1024
    SCHEDULING_HEADROOM_KIB = 128 * 1024

    def __init__(self, faulty_service: str = "reservation"):
        self.app = HotelReservation()
        super().__init__(app=self.app, namespace=self.app.namespace)
        self.namespace = self.app.namespace
        self.faulty_service = faulty_service
        self.kubectl = KubeCtl()
        self.apps_v1 = client.AppsV1Api()
        self.core_v1 = client.CoreV1Api()
        self.scheduling_v1 = client.SchedulingV1Api()
        self.target_node = None
        self.target_request_memory = None
        self.pressure_request_memory = None
        self._app_cleanup = self.app.cleanup
        self.app.cleanup = self._cleanup

        self.root_cause = self.build_structured_root_cause(
            component=f"PriorityClass/{self.PLATFORM_PRIORITY_CLASS}",
            namespace=self.namespace,
            description=(
                f"PriorityClass `{self.PLATFORM_PRIORITY_CLASS}` has been made the cluster-wide global default. "
                f"Existing `{self.faulty_service}` pods in namespace `{self.namespace}` were created before that "
                "default existed, so they have priority 0. A new tenant workload in namespace "
                f"`{self.PRESSURE_NAMESPACE}` receives `{self.PLATFORM_PRIORITY_CLASS}` and requests enough memory "
                "on the same node to trigger scheduler preemption. The scheduler evicts the lower-priority "
                f"`{self.faulty_service}` pod, but replacement production pods inherit the same medium priority "
                "instead of the intended `production-critical` class and cannot preempt the tenant workload back. "
                "The service stays under-replicated even though its image, service, and application config are valid. "
                f"Mitigation must make `{self.PLATFORM_PRIORITY_CLASS}` no longer an unsafe global default and "
                f"explicitly protect `{self.faulty_service}` with `{self.PRODUCTION_PRIORITY_CLASS}`."
            ),
        )

        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)
        self.app.create_workload()
        self.mitigation_oracle = PriorityPreemptionMitigationOracle(problem=self)

    def _target_deployment(self):
        return self.apps_v1.read_namespaced_deployment(name=self.faulty_service, namespace=self.namespace)

    def _target_container_name(self):
        return self._target_deployment().spec.template.spec.containers[0].name

    def _target_pod(self):
        deployment = self._target_deployment()
        match_labels = deployment.spec.selector.match_labels or {}
        selector = ",".join(f"{key}={value}" for key, value in match_labels.items())
        pods = self.core_v1.list_namespaced_pod(
            namespace=self.namespace,
            label_selector=selector,
        ).items
        running = [pod for pod in pods if pod.status.phase == "Running" and pod.spec.node_name]
        if not running:
            raise RuntimeError(f"No running pod found for service '{self.faulty_service}'")
        return running[0]

    def _active_pods_on_node(self, node_name):
        pods = self.core_v1.list_pod_for_all_namespaces(field_selector=f"spec.nodeName={node_name}").items
        return [pod for pod in pods if pod.status.phase not in {"Succeeded", "Failed"}]

    def _pod_memory_request_kib(self, pod):
        total = 0
        for container in pod.spec.containers or []:
            resources = container.resources
            if not resources or not resources.requests:
                continue
            memory = resources.requests.get("memory")
            if memory:
                total += self._memory_quantity_to_kib(memory)
        return total

    def _memory_quantity_to_kib(self, quantity):
        return int(parse_quantity(str(quantity)) / 1024)

    def _node_allocatable_memory_kib(self, node_name):
        node = self.core_v1.read_node(node_name)
        return self._memory_quantity_to_kib(node.status.allocatable["memory"])

    def _node_requested_memory_kib(self, node_name):
        return sum(self._pod_memory_request_kib(pod) for pod in self._active_pods_on_node(node_name))

    def _wait_for_deployment_ready(self, name, namespace, timeout=180):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            deployment = self.apps_v1.read_namespaced_deployment(name=name, namespace=namespace)
            desired = deployment.spec.replicas or 0
            observed = deployment.status.observed_generation or 0
            generation = deployment.metadata.generation or 0
            updated = deployment.status.updated_replicas or 0
            ready = deployment.status.ready_replicas or 0
            available = deployment.status.available_replicas or 0
            unavailable = deployment.status.unavailable_replicas or 0
            if (
                desired > 0
                and observed >= generation
                and updated == desired
                and ready == desired
                and available == desired
                and unavailable == 0
            ):
                return deployment
            time.sleep(2)
        raise TimeoutError(f"Timed out waiting for deployment {namespace}/{name} to become ready")

    def _wait_for_preemption(self, timeout=180):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            target = self.apps_v1.read_namespaced_deployment(self.faulty_service, self.namespace)
            pressure = self.apps_v1.read_namespaced_deployment(self.PRESSURE_DEPLOYMENT, self.PRESSURE_NAMESPACE)
            target_desired = target.spec.replicas or 0
            target_ready = target.status.ready_replicas or 0
            pressure_ready = pressure.status.ready_replicas or 0
            if pressure_ready >= 1 and target_ready < target_desired:
                return
            time.sleep(3)
        events = self.kubectl.exec_command(f"kubectl get events -n {self.namespace} --sort-by=.lastTimestamp")
        raise TimeoutError(f"Timed out waiting for priority preemption to manifest. Recent app events:\n{events}")

    def _target_request_for_node(self, node_name):
        allocatable_kib = self._node_allocatable_memory_kib(node_name)
        requested_kib = self._node_requested_memory_kib(node_name)
        free_kib = max(0, allocatable_kib - requested_kib)
        target_ceiling_kib = free_kib - self.SCHEDULING_HEADROOM_KIB
        if target_ceiling_kib < self.MIN_TARGET_REQUEST_KIB:
            raise RuntimeError(
                "Cannot inject priority preemption cascade because the target node does not have enough "
                f"request headroom. Node={node_name}, allocatable={self.kubectl.format_k8s_memory(allocatable_kib)}, "
                f"requested={self.kubectl.format_k8s_memory(requested_kib)}"
            )

        target_kib = max(self.MIN_TARGET_REQUEST_KIB, int(allocatable_kib * self.TARGET_REQUEST_RATIO))
        target_kib = min(target_kib, target_ceiling_kib)
        return self.kubectl.format_k8s_memory(target_kib)

    def _pressure_request_for_target_pod(self, target_pod):
        node_name = target_pod.spec.node_name
        allocatable_kib = self._node_allocatable_memory_kib(node_name)
        requested_kib = self._node_requested_memory_kib(node_name)
        free_kib = max(0, allocatable_kib - requested_kib)
        target_request_kib = self._pod_memory_request_kib(target_pod)
        if target_request_kib <= self.MIN_PREEMPTION_GAP_KIB:
            raise RuntimeError(
                f"Target pod {target_pod.metadata.name} has too little memory request "
                "to make scheduler preemption deterministic"
            )

        headroom_kib = min(self.SCHEDULING_HEADROOM_KIB, max(1, target_request_kib // 4))
        pressure_ceiling_kib = free_kib + target_request_kib - headroom_kib
        if pressure_ceiling_kib <= free_kib:
            raise RuntimeError(
                f"Target pod {target_pod.metadata.name} does not free enough requested memory for pressure workload"
            )

        preemption_gap_kib = max(
            self.MIN_PREEMPTION_GAP_KIB,
            int(target_request_kib * self.PRESSURE_PREEMPTION_RATIO),
        )
        preemption_gap_kib = min(preemption_gap_kib, pressure_ceiling_kib - free_kib)
        pressure_kib = free_kib + preemption_gap_kib
        if pressure_ceiling_kib >= self.MIN_PRESSURE_REQUEST_KIB:
            pressure_kib = max(self.MIN_PRESSURE_REQUEST_KIB, pressure_kib)
        if pressure_kib <= free_kib:
            raise RuntimeError(
                "Pressure workload would fit without preemption; refusing to inject a non-deterministic fault"
            )
        return self.kubectl.format_k8s_memory(pressure_kib)

    def _patch_target_requests(self):
        container_name = self._target_container_name()
        deployment = self._target_deployment()
        container = next(
            (container for container in deployment.spec.template.spec.containers if container.name == container_name),
            None,
        )
        resources = {
            "requests": {
                "cpu": "25m",
                "memory": self.target_request_memory,
            }
        }
        if container and container.resources and container.resources.limits:
            limits = dict(container.resources.limits)
            memory_limit = limits.get("memory")
            if memory_limit and self._memory_quantity_to_kib(memory_limit) < self._memory_quantity_to_kib(
                self.target_request_memory
            ):
                limits["memory"] = self.target_request_memory
                resources["limits"] = limits

        body = {
            "spec": {
                "template": {
                    "spec": {
                        "containers": [
                            {
                                "name": container_name,
                                "resources": resources,
                            }
                        ]
                    }
                }
            }
        }
        self.apps_v1.patch_namespaced_deployment(name=self.faulty_service, namespace=self.namespace, body=body)
        self._wait_for_deployment_ready(self.faulty_service, self.namespace)

    def _protect_peer_deployments(self):
        peer_names = [
            deployment.metadata.name
            for deployment in self.apps_v1.list_namespaced_deployment(self.namespace).items
            if deployment.metadata.name != self.faulty_service
        ]
        body = {"spec": {"template": {"spec": {"priorityClassName": self.PLATFORM_PRIORITY_CLASS}}}}
        for name in peer_names:
            self.apps_v1.patch_namespaced_deployment(name=name, namespace=self.namespace, body=body)
        for name in peer_names:
            self._wait_for_deployment_ready(name, self.namespace)

    def _ensure_target_preemptable(self, target_pod):
        priority = target_pod.spec.priority or 0
        priority_class = target_pod.spec.priority_class_name
        if priority_class or priority >= 100000:
            raise RuntimeError(
                f"Target pod {target_pod.metadata.name} is no longer the low-priority preemption victim "
                f"(priorityClassName={priority_class}, priority={priority})"
            )

    def _create_or_replace_priority_class(self, name, value, global_default):
        if global_default:
            existing_defaults = [
                pc.metadata.name
                for pc in self.scheduling_v1.list_priority_class().items
                if pc.global_default and pc.metadata.name != name
            ]
            if existing_defaults:
                raise RuntimeError(
                    "Cannot inject priority preemption cascade because another global default "
                    f"PriorityClass already exists: {existing_defaults}"
                )

        body = client.V1PriorityClass(
            metadata=client.V1ObjectMeta(name=name),
            value=value,
            global_default=global_default,
            preemption_policy="PreemptLowerPriority",
            description="SREGym priority preemption cascade simulation.",
        )
        try:
            self.scheduling_v1.create_priority_class(body)
        except ApiException as e:
            if e.status != 409:
                raise
            existing = self.scheduling_v1.read_priority_class(name)
            if existing.value != value:
                raise RuntimeError(
                    f"PriorityClass '{name}' already exists with immutable value {existing.value}; " f"expected {value}"
                ) from e
            body.metadata.resource_version = existing.metadata.resource_version
            self.scheduling_v1.replace_priority_class(name=name, body=body)

    def _delete_priority_class(self, name):
        try:
            self.scheduling_v1.delete_priority_class(name)
        except ApiException as e:
            if e.status != 404:
                raise

    def _ensure_namespace(self, name):
        body = client.V1Namespace(metadata=client.V1ObjectMeta(name=name))
        try:
            self.core_v1.create_namespace(body)
        except ApiException as e:
            if e.status != 409:
                raise

    def _create_pressure_deployment(self):
        self._ensure_namespace(self.PRESSURE_NAMESPACE)
        body = {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": self.PRESSURE_DEPLOYMENT, "namespace": self.PRESSURE_NAMESPACE},
            "spec": {
                "replicas": 1,
                "selector": {"matchLabels": {"app": self.PRESSURE_LABEL}},
                "template": {
                    "metadata": {"labels": {"app": self.PRESSURE_LABEL, "workload": "analytics-import"}},
                    "spec": {
                        "priorityClassName": self.PLATFORM_PRIORITY_CLASS,
                        "nodeSelector": {"kubernetes.io/hostname": self.target_node},
                        "terminationGracePeriodSeconds": 0,
                        "containers": [
                            {
                                "name": "worker",
                                "image": "registry.k8s.io/pause:3.9",
                                "resources": {
                                    "requests": {
                                        "cpu": "25m",
                                        "memory": self.pressure_request_memory,
                                    }
                                },
                            }
                        ],
                    },
                },
            },
        }
        try:
            self.apps_v1.create_namespaced_deployment(namespace=self.PRESSURE_NAMESPACE, body=body)
        except ApiException as e:
            if e.status != 409:
                raise
            self.apps_v1.replace_namespaced_deployment(
                name=self.PRESSURE_DEPLOYMENT,
                namespace=self.PRESSURE_NAMESPACE,
                body=body,
            )

    def _delete_pressure_namespace(self):
        try:
            self.core_v1.delete_namespace(self.PRESSURE_NAMESPACE)
        except ApiException as e:
            if e.status != 404:
                raise
            return

        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            try:
                self.core_v1.read_namespace(self.PRESSURE_NAMESPACE)
            except ApiException as e:
                if e.status == 404:
                    return
                raise
            time.sleep(2)

    def _delete_support_resources(self):
        with contextlib.suppress(Exception):
            self._delete_pressure_namespace()
        with contextlib.suppress(Exception):
            self._delete_priority_class(self.PLATFORM_PRIORITY_CLASS)
        with contextlib.suppress(Exception):
            self._delete_priority_class(self.PRODUCTION_PRIORITY_CLASS)

    def _cleanup(self):
        self._delete_support_resources()
        self._app_cleanup()

    def _make_platform_priority_safe(self):
        self._create_or_replace_priority_class(self.PLATFORM_PRIORITY_CLASS, value=100000, global_default=False)

    def _protect_target_deployment(self):
        body = {"spec": {"template": {"spec": {"priorityClassName": self.PRODUCTION_PRIORITY_CLASS}}}}
        self.apps_v1.patch_namespaced_deployment(name=self.faulty_service, namespace=self.namespace, body=body)

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")
        self._delete_support_resources()

        target_pod = self._target_pod()
        self.target_node = target_pod.spec.node_name
        self.target_request_memory = self._target_request_for_node(self.target_node)
        print(f"Target node: {self.target_node} | target request: {self.target_request_memory}")

        print(f"Preparing existing production pod '{self.faulty_service}' with realistic memory requests")
        self._patch_target_requests()
        target_pod = self._target_pod()
        self.target_node = target_pod.spec.node_name

        print("Creating unsafe PriorityClasses")
        self._create_or_replace_priority_class(self.PLATFORM_PRIORITY_CLASS, value=100000, global_default=True)
        self._create_or_replace_priority_class(self.PRODUCTION_PRIORITY_CLASS, value=200000, global_default=False)

        print("Protecting peer app deployments so the scheduler has a deterministic victim")
        self._protect_peer_deployments()
        target_pod = self._target_pod()
        self._ensure_target_preemptable(target_pod)
        self.target_node = target_pod.spec.node_name
        self.pressure_request_memory = self._pressure_request_for_target_pod(target_pod)
        print(f"Pressure node: {self.target_node} | pressure request: {self.pressure_request_memory}")

        print(f"Creating tenant pressure workload in namespace '{self.PRESSURE_NAMESPACE}'")
        self._create_pressure_deployment()
        self._wait_for_preemption()

        print(
            f"Priority preemption cascade injected: '{self.PRESSURE_DEPLOYMENT}' preempted "
            f"'{self.faulty_service}' on node '{self.target_node}'."
        )
        print(f"Service: {self.faulty_service} | Namespace: {self.namespace}\n")

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")
        self._make_platform_priority_safe()
        self._protect_target_deployment()
        self._wait_for_deployment_ready(self.faulty_service, self.namespace)
        print(
            f"Recovered priority preemption cascade by protecting "
            f"{self.namespace}/{self.faulty_service} with {self.PRODUCTION_PRIORITY_CLASS}\n"
        )
