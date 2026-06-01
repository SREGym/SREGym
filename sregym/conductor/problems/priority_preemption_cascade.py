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
    RESOURCE_LABEL_KEY = "app.kubernetes.io/part-of"
    RESOURCE_LABEL_VALUE = "priority-policy-rollout"

    TARGET_REQUEST_RATIO = 0.30
    TARGET_REQUEST_CAP_KIB = 8 * 1024 * 1024
    PRESSURE_PREEMPTION_RATIO = 0.50
    MIN_TARGET_REQUEST_KIB = 256 * 1024
    MIN_PRESSURE_REQUEST_KIB = 512 * 1024
    MIN_PREEMPTION_GAP_KIB = 64 * 1024
    SCHEDULING_HEADROOM_KIB = 128 * 1024
    DESIRED_FREE_MEMORY_KIB = 4 * 1024 * 1024
    PADDING_REQUEST_KIB = 16 * 1024 * 1024
    MIN_PADDING_REQUEST_KIB = 8 * 1024 * 1024
    PADDING_DEPLOYMENT_PREFIX = "report-cache-shard"
    PADDING_LABEL = "report-cache"

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
        self._priority_class_snapshots = {}
        self._deployment_priority_classes = {}
        self._target_original_resources = None
        self._target_original_node_selector = None
        self._app_cleanup = self.app.cleanup
        self.app.cleanup = self._cleanup

        self.root_cause = self.build_structured_root_cause(
            component=f"PriorityClass/{self.PLATFORM_PRIORITY_CLASS}",
            namespace=self.namespace,
            description=(
                "A cluster-wide default PriorityClass was mis-scoped so an existing production pod kept priority 0 "
                "while a newly created tenant or batch workload received a higher priority and enough memory request "
                "to force scheduler preemption on the same node. The scheduler evicts the lower-priority production "
                "pod, but its replacement inherits the same medium/default priority relationship instead of the "
                "intended higher production priority and cannot reclaim capacity from the tenant workload. "
                "The service stays under-replicated even though its image, service, and application config are valid. "
                f"Mitigation must make `{self.PLATFORM_PRIORITY_CLASS}` no longer an unsafe global default and "
                f"explicitly protect `{self.faulty_service}` with a higher-valued production PriorityClass."
            ),
        )

        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)
        self.app.create_workload()
        self.mitigation_oracle = PriorityPreemptionMitigationOracle(problem=self)

    def _target_deployment(self):
        return self.apps_v1.read_namespaced_deployment(name=self.faulty_service, namespace=self.namespace)

    def _app_deployments(self):
        try:
            return self.apps_v1.list_namespaced_deployment(self.namespace).items
        except ApiException as e:
            if e.status == 404:
                return []
            raise

    def _target_container_name(self):
        return self._target_deployment().spec.template.spec.containers[0].name

    def _target_pods(self):
        deployment = self._target_deployment()
        match_labels = deployment.spec.selector.match_labels or {}
        selector = ",".join(f"{key}={value}" for key, value in match_labels.items())
        return self.core_v1.list_namespaced_pod(
            namespace=self.namespace,
            label_selector=selector,
        ).items

    def _target_pod(self):
        pods = self._target_pods()
        running = [pod for pod in pods if pod.status.phase == "Running" and pod.spec.node_name]
        if not running:
            raise RuntimeError(f"No running pod found for service '{self.faulty_service}'")
        return running[0]

    def _pressure_pods(self):
        try:
            return self.core_v1.list_namespaced_pod(
                namespace=self.PRESSURE_NAMESPACE,
                label_selector=f"app={self.PRESSURE_LABEL}",
            ).items
        except ApiException as e:
            if e.status == 404:
                return []
            raise

    def _wait_for_pressure_pod(self, timeout=60):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            pods = self._pressure_pods()
            if pods:
                return pods[0]
            time.sleep(2)
        raise TimeoutError(f"Timed out waiting for pressure pod in namespace '{self.PRESSURE_NAMESPACE}'")

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

    def _preemption_event_seen(self):
        events = self.core_v1.list_event_for_all_namespaces().items
        for event in events:
            reason = (event.reason or "").lower()
            message = (event.message or "").lower()
            involved = event.involved_object
            involved_name = (involved.name if involved else "") or ""
            involved_namespace = (involved.namespace if involved else "") or ""
            event_text = " ".join(
                [
                    reason,
                    message,
                    involved_name.lower(),
                    involved_namespace.lower(),
                ]
            )
            if "preempt" not in event_text:
                continue
            if (
                self.faulty_service in event_text
                or self.PRESSURE_DEPLOYMENT in event_text
                or self.PRESSURE_LABEL in event_text
            ):
                return True
        return False

    def _replacement_target_has_platform_priority(self):
        for pod in self._target_pods():
            priority_class = pod.spec.priority_class_name
            priority = pod.spec.priority or 0
            if pod.status.phase == "Running" and pod.spec.node_name:
                continue
            if priority_class == self.PLATFORM_PRIORITY_CLASS and priority >= 100000:
                return True
        return False

    def _preemption_evidence_ready(self, target, pressure):
        target_desired = target.spec.replicas or 0
        target_ready = target.status.ready_replicas or 0
        pressure_ready = pressure.status.ready_replicas or 0
        return (
            pressure_ready >= 1
            and target_ready < target_desired
            and self._preemption_event_seen()
            and self._replacement_target_has_platform_priority()
        )

    def _wait_for_preemption(self, timeout=180):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            target = self.apps_v1.read_namespaced_deployment(self.faulty_service, self.namespace)
            pressure = self.apps_v1.read_namespaced_deployment(self.PRESSURE_DEPLOYMENT, self.PRESSURE_NAMESPACE)
            if self._preemption_evidence_ready(target, pressure):
                return
            time.sleep(3)
        events = self.kubectl.exec_command("kubectl get events -A --sort-by=.lastTimestamp")
        raise TimeoutError(
            "Timed out waiting for priority preemption evidence: pressure pod ready, target unavailable, "
            f"scheduler preemption event present, and replacement target pod inheriting {self.PLATFORM_PRIORITY_CLASS}. "
            f"Recent events:\n{events}"
        )

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
        target_kib = min(target_kib, self.TARGET_REQUEST_CAP_KIB, target_ceiling_kib)
        return self.kubectl.format_k8s_memory(target_kib)

    def _padding_request_sizes_kib(self, free_kib, desired_free_kib=None):
        desired_free_kib = self.DESIRED_FREE_MEMORY_KIB if desired_free_kib is None else desired_free_kib
        reserve_kib = max(0, free_kib - desired_free_kib)
        request_sizes = []
        while reserve_kib >= self.PADDING_REQUEST_KIB:
            request_sizes.append(self.PADDING_REQUEST_KIB)
            reserve_kib -= self.PADDING_REQUEST_KIB
        if reserve_kib >= self.MIN_PADDING_REQUEST_KIB:
            request_sizes.append(self.MIN_PADDING_REQUEST_KIB)
        return request_sizes

    def _padding_request_sizes_for_node(self, node_name):
        allocatable_kib = self._node_allocatable_memory_kib(node_name)
        requested_kib = self._node_requested_memory_kib(node_name)
        free_kib = max(0, allocatable_kib - requested_kib)
        return self._padding_request_sizes_kib(free_kib)

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

    def _capture_app_template_state(self):
        self._deployment_priority_classes = {}
        self._target_original_resources = None
        self._target_original_node_selector = None
        for deployment in self._app_deployments():
            self._deployment_priority_classes[deployment.metadata.name] = (
                deployment.spec.template.spec.priority_class_name
            )
            if deployment.metadata.name != self.faulty_service:
                continue
            self._target_original_node_selector = dict(deployment.spec.template.spec.node_selector or {})
            target_container = deployment.spec.template.spec.containers[0]
            resources = target_container.resources
            self._target_original_resources = {
                "requests": dict(resources.requests or {}) if resources and resources.requests else {},
                "limits": dict(resources.limits or {}) if resources and resources.limits else {},
            }

    def _restore_app_template_state(self):
        for deployment in self._app_deployments():
            pod_spec = deployment.spec.template.spec
            desired_priority = self._deployment_priority_classes.get(deployment.metadata.name)
            current_priority = pod_spec.priority_class_name
            body = {"spec": {"template": {"spec": {}}}}
            if current_priority != desired_priority:
                body["spec"]["template"]["spec"]["priorityClassName"] = desired_priority

            if deployment.metadata.name == self.faulty_service and self._target_original_resources is not None:
                resources = {}
                if self._target_original_resources["requests"]:
                    resources["requests"] = dict(self._target_original_resources["requests"])
                if self._target_original_resources["limits"]:
                    resources["limits"] = dict(self._target_original_resources["limits"])
                body["spec"]["template"]["spec"]["nodeSelector"] = self._target_original_node_selector or None
                body["spec"]["template"]["spec"]["containers"] = [
                    {
                        "name": pod_spec.containers[0].name,
                        "resources": resources,
                    }
                ]

            if body["spec"]["template"]["spec"]:
                self.apps_v1.patch_namespaced_deployment(
                    name=deployment.metadata.name,
                    namespace=self.namespace,
                    body=body,
                )

    def _pin_target_to_node(self):
        deployment = self._target_deployment()
        node_selector = dict(deployment.spec.template.spec.node_selector or {})
        node_selector["kubernetes.io/hostname"] = self.target_node
        body = {"spec": {"template": {"spec": {"nodeSelector": node_selector}}}}
        self.apps_v1.patch_namespaced_deployment(name=self.faulty_service, namespace=self.namespace, body=body)
        self._wait_for_deployment_ready(self.faulty_service, self.namespace)

    def _clear_app_priority_references(self):
        for deployment in self._app_deployments():
            if not deployment.spec.template.spec.priority_class_name:
                continue
            body = {"spec": {"template": {"spec": {"priorityClassName": None}}}}
            self.apps_v1.patch_namespaced_deployment(
                name=deployment.metadata.name,
                namespace=self.namespace,
                body=body,
            )

    def _wait_for_priority_references_removed(self, timeout=60):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            references = [
                deployment.metadata.name
                for deployment in self._app_deployments()
                if deployment.spec.template.spec.priority_class_name
                in {self.PLATFORM_PRIORITY_CLASS, self.PRODUCTION_PRIORITY_CLASS}
            ]
            if not references:
                return
            time.sleep(2)
        raise TimeoutError("Timed out waiting for app deployments to drop problem PriorityClass references")

    def _protect_peer_deployments(self):
        peer_names = [
            deployment.metadata.name
            for deployment in self._app_deployments()
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
        if target_pod.spec.node_name != self.target_node:
            raise RuntimeError(
                f"Target pod {target_pod.metadata.name} moved from pressure node {self.target_node} "
                f"to {target_pod.spec.node_name}; refusing to inject a non-deterministic preemption fault"
            )

    def _ensure_pressure_can_preempt_target(self, pressure_pod, target_pod):
        pressure_priority = pressure_pod.spec.priority or 0
        target_priority = target_pod.spec.priority or 0
        if pressure_priority <= target_priority:
            raise RuntimeError(
                f"Pressure pod {pressure_pod.metadata.name} priority {pressure_priority} is not higher than "
                f"target pod {target_pod.metadata.name} priority {target_priority}"
            )
        if pressure_pod.spec.priority_class_name != self.PLATFORM_PRIORITY_CLASS:
            raise RuntimeError(
                f"Pressure pod {pressure_pod.metadata.name} did not receive PriorityClass "
                f"{self.PLATFORM_PRIORITY_CLASS}"
            )

    def _create_or_replace_priority_class(self, name, value, global_default, description):
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
            metadata=client.V1ObjectMeta(
                name=name,
                labels={self.RESOURCE_LABEL_KEY: self.RESOURCE_LABEL_VALUE},
            ),
            value=value,
            global_default=global_default,
            preemption_policy="PreemptLowerPriority",
            description=description,
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

    def _capture_priority_classes(self):
        self._priority_class_snapshots = {}
        for name in [self.PLATFORM_PRIORITY_CLASS, self.PRODUCTION_PRIORITY_CLASS]:
            try:
                existing = self.scheduling_v1.read_priority_class(name)
            except ApiException as e:
                if e.status == 404:
                    self._priority_class_snapshots[name] = None
                    continue
                raise

            self._priority_class_snapshots[name] = {
                "value": existing.value,
                "global_default": bool(existing.global_default),
                "preemption_policy": existing.preemption_policy,
                "description": existing.description,
                "labels": dict(existing.metadata.labels or {}),
            }

    def _priority_class_has_problem_label(self, name):
        try:
            priority_class = self.scheduling_v1.read_priority_class(name)
        except ApiException as e:
            if e.status == 404:
                return False
            raise
        labels = priority_class.metadata.labels or {}
        return labels.get(self.RESOURCE_LABEL_KEY) == self.RESOURCE_LABEL_VALUE

    def _restore_or_delete_priority_class(self, name):
        has_snapshot = name in self._priority_class_snapshots
        snapshot = self._priority_class_snapshots.get(name)
        if snapshot:
            body = client.V1PriorityClass(
                metadata=client.V1ObjectMeta(name=name, labels=snapshot["labels"]),
                value=snapshot["value"],
                global_default=snapshot["global_default"],
                preemption_policy=snapshot["preemption_policy"],
                description=snapshot["description"],
            )
            try:
                existing = self.scheduling_v1.read_priority_class(name)
            except ApiException as e:
                if e.status == 404:
                    self.scheduling_v1.create_priority_class(body)
                    return
                raise
            body.metadata.resource_version = existing.metadata.resource_version
            self.scheduling_v1.replace_priority_class(name=name, body=body)
            return

        if (has_snapshot and snapshot is None) or self._priority_class_has_problem_label(name):
            self._delete_priority_class(name)

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

    def _create_padding_deployment(self, index, request_memory):
        name = f"{self.PADDING_DEPLOYMENT_PREFIX}-{index}"
        labels = {"app": self.PADDING_LABEL, "shard": str(index)}
        body = {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": name, "namespace": self.PRESSURE_NAMESPACE},
            "spec": {
                "replicas": 1,
                "selector": {"matchLabels": labels},
                "template": {
                    "metadata": {"labels": {**labels, "workload": "analytics-cache"}},
                    "spec": {
                        "priorityClassName": self.PLATFORM_PRIORITY_CLASS,
                        "nodeSelector": {"kubernetes.io/hostname": self.target_node},
                        "terminationGracePeriodSeconds": 0,
                        "containers": [
                            {
                                "name": "cache",
                                "image": "registry.k8s.io/pause:3.9",
                                "resources": {
                                    "requests": {
                                        "cpu": "10m",
                                        "memory": request_memory,
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
                name=name,
                namespace=self.PRESSURE_NAMESPACE,
                body=body,
            )
        return name

    def _create_capacity_padding(self):
        request_sizes_kib = self._padding_request_sizes_for_node(self.target_node)
        if not request_sizes_kib:
            return []

        self._ensure_namespace(self.PRESSURE_NAMESPACE)
        padding_names = []
        try:
            for index, request_kib in enumerate(request_sizes_kib):
                request_memory = self.kubectl.format_k8s_memory(request_kib)
                padding_names.append(self._create_padding_deployment(index, request_memory))
            for name in padding_names:
                self._wait_for_deployment_ready(name, self.PRESSURE_NAMESPACE)
        except Exception:
            with contextlib.suppress(Exception):
                self._delete_pressure_namespace()
            raise
        return padding_names

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
            self._restore_app_template_state()
        with contextlib.suppress(Exception):
            self._clear_app_priority_references()
        priority_references_removed = False
        try:
            self._wait_for_priority_references_removed()
            priority_references_removed = True
        except Exception:
            pass
        with contextlib.suppress(Exception):
            self._delete_pressure_namespace()
        if priority_references_removed:
            with contextlib.suppress(Exception):
                self._restore_or_delete_priority_class(self.PLATFORM_PRIORITY_CLASS)
            with contextlib.suppress(Exception):
                self._restore_or_delete_priority_class(self.PRODUCTION_PRIORITY_CLASS)

    def _cleanup(self):
        self._delete_support_resources()
        self._app_cleanup()

    def _make_platform_priority_safe(self):
        self._create_or_replace_priority_class(
            self.PLATFORM_PRIORITY_CLASS,
            value=100000,
            global_default=False,
            description="Default priority for shared platform and tenant workloads.",
        )

    def _protect_target_deployment(self):
        body = {"spec": {"template": {"spec": {"priorityClassName": self.PRODUCTION_PRIORITY_CLASS}}}}
        self.apps_v1.patch_namespaced_deployment(name=self.faulty_service, namespace=self.namespace, body=body)

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")
        self._delete_support_resources()
        self._capture_priority_classes()

        target_pod = self._target_pod()
        self.target_node = target_pod.spec.node_name
        self.target_request_memory = self._target_request_for_node(self.target_node)
        self._capture_app_template_state()
        print(f"Target node: {self.target_node} | target request: {self.target_request_memory}")

        print(f"Preparing existing production pod '{self.faulty_service}' with realistic memory requests")
        self._patch_target_requests()
        target_pod = self._target_pod()
        self.target_node = target_pod.spec.node_name
        print(f"Pinning '{self.faulty_service}' to pressure node '{self.target_node}'")
        self._pin_target_to_node()
        target_pod = self._target_pod()
        self.target_node = target_pod.spec.node_name
        self._ensure_target_preemptable(target_pod)

        print("Creating unsafe PriorityClasses")
        self._create_or_replace_priority_class(
            self.PLATFORM_PRIORITY_CLASS,
            value=100000,
            global_default=True,
            description="Default priority for shared platform and tenant workloads.",
        )
        self._create_or_replace_priority_class(
            self.PRODUCTION_PRIORITY_CLASS,
            value=200000,
            global_default=False,
            description=f"Priority for protected {self.faulty_service} production workloads.",
        )

        print("Protecting peer app deployments so the scheduler has a deterministic victim")
        self._protect_peer_deployments()
        target_pod = self._target_pod()
        self._ensure_target_preemptable(target_pod)
        self.target_node = target_pod.spec.node_name
        padding_names = self._create_capacity_padding()
        try:
            if padding_names:
                print(
                    f"Created {len(padding_names)} cache padding workload(s) with the tenant pressure PriorityClass "
                    f"on pressure node '{self.target_node}'"
                )
            self.pressure_request_memory = self._pressure_request_for_target_pod(target_pod)
            print(f"Pressure node: {self.target_node} | pressure request: {self.pressure_request_memory}")

            print(f"Creating tenant pressure workload in namespace '{self.PRESSURE_NAMESPACE}'")
            self._create_pressure_deployment()
            pressure_pod = self._wait_for_pressure_pod()
            self._ensure_pressure_can_preempt_target(pressure_pod, target_pod)
            self._wait_for_preemption()
        except Exception:
            with contextlib.suppress(Exception):
                self._delete_pressure_namespace()
            raise

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
