"""Mitigation oracle for PriorityClass preemption cascade failures."""

import time

from kubernetes import client
from kubernetes.client.exceptions import ApiException
from kubernetes.utils.quantity import parse_quantity

from sregym.conductor.oracles.base import Oracle
from sregym.conductor.oracles.failure import FailureClass

_ROLLOUT_SETTLE_SECONDS = 60
_ROLLOUT_POLL_INTERVAL = 5


class PriorityPreemptionMitigationOracle(Oracle):
    """Verify that app recovery came from safe priority policy, not demolition.

    A plain pod-health oracle is too weak for scheduler-priority faults: an agent
    can delete the pressure workload or remove all PriorityClasses and make the
    app look healthy without correcting the bad priority relationship. This
    oracle requires the production workload to be protected by a higher-valued
    PriorityClass and the unsafe global default to be removed while keeping the
    synthetic tenant workload present.
    """

    importance = 1.0

    # This oracle exists because a pod-health check is too weak here: the agent
    # can make the app look healthy by demolition rather than by fixing the
    # priority relationship. Every reason below is one of those demolition or
    # gaming moves, which is why they are all AGENT_ERROR -- each is an action
    # somebody took, and the fault injection took none of them.
    FAILURE_CLASSES = {
        "platform_priority_class_deleted": FailureClass.AGENT_ERROR,
        "platform_priority_still_global_default": FailureClass.AGENT_ERROR,
        "target_has_no_priority_class": FailureClass.AGENT_ERROR,
        "target_priority_class_missing": FailureClass.AGENT_ERROR,
        "target_priority_not_above_platform": FailureClass.AGENT_ERROR,
        "memory_request_reduced": FailureClass.AGENT_ERROR,
        "pressure_workload_deleted": FailureClass.AGENT_ERROR,
        "pressure_workload_scaled_to_zero": FailureClass.AGENT_ERROR,
    }

    def __init__(self, problem):
        super().__init__(problem)
        self.apps_v1 = client.AppsV1Api()
        self.core_v1 = client.CoreV1Api()
        self.scheduling_v1 = client.SchedulingV1Api()

    def _wait_for_rollouts(self, namespace):
        deadline = time.monotonic() + _ROLLOUT_SETTLE_SECONDS
        while time.monotonic() < deadline:
            deployments = self.apps_v1.list_namespaced_deployment(namespace)
            all_settled = True
            for dep in deployments.items:
                desired = dep.spec.replicas or 0
                status = dep.status
                if (
                    (status.updated_replicas or 0) < desired
                    or (status.ready_replicas or 0) < desired
                    or (status.unavailable_replicas or 0) > 0
                ):
                    all_settled = False
                    break
            if all_settled:
                return
            time.sleep(_ROLLOUT_POLL_INTERVAL)
        print("⚠️ Timed out waiting for deployments to settle; evaluating current state")

    def _read_priority_class(self, name):
        try:
            return self.scheduling_v1.read_priority_class(name)
        except ApiException as e:
            if e.status == 404:
                return None
            raise

    def _deployment_unready(self, name, namespace):
        """Return ``(verdict_or_None, deployment)``.

        The helpers in this oracle used to return bare booleans, printing the
        reason and discarding it. They now return a verdict or ``None`` so the
        reason survives to the caller; the prints are unchanged, since they
        carry names and counts that the reason code deliberately does not.
        """
        try:
            deployment = self.apps_v1.read_namespaced_deployment(name=name, namespace=namespace)
        except ApiException as e:
            if e.status == 404:
                print(f"❌ Deployment '{name}' not found in namespace '{namespace}'")
                return self.fail("required_deployment_missing", deployment=name, namespace=namespace), None
            raise

        desired = deployment.spec.replicas or 0
        ready = deployment.status.ready_replicas or 0
        if desired < 1:
            print(f"❌ Deployment '{name}' has invalid desired replica count: {desired}")
            return self.fail("invalid_replica_count", deployment=name, desired=desired), deployment
        if ready != desired:
            print(f"❌ Deployment '{name}' has {ready}/{desired} replicas ready")
            return (
                self.fail("deployment_replicas_unready", deployment=name, ready=ready, desired=desired),
                deployment,
            )
        return None, deployment

    def _any_deployment_unready(self, namespace):
        try:
            deployments = self.apps_v1.list_namespaced_deployment(namespace).items
        except ApiException as e:
            if e.status == 404:
                print(f"❌ Namespace '{namespace}' not found")
                return self.fail("namespace_missing", namespace=namespace)
            raise

        if not deployments:
            print(f"❌ No deployments found in namespace '{namespace}'")
            return self.fail("no_deployments_found", namespace=namespace)

        for deployment in deployments:
            name = deployment.metadata.name
            desired = deployment.spec.replicas or 0
            ready = deployment.status.ready_replicas or 0
            if desired < 1:
                print(f"❌ Deployment '{name}' was scaled below one replica")
                return self.fail("required_deployment_scaled_to_zero", deployment=name)
            if ready != desired:
                print(f"❌ Deployment '{name}' has {ready}/{desired} replicas ready")
                return self.fail("deployment_replicas_unready", deployment=name, ready=ready, desired=desired)
        return None

    def _service_endpoint_unready(self, service_name, namespace):
        try:
            endpoints = self.core_v1.read_namespaced_endpoints(service_name, namespace)
        except ApiException as e:
            if e.status == 404:
                print(f"❌ Service '{service_name}' endpoints not found in namespace '{namespace}'")
                return self.fail("no_ready_endpoints", service=service_name, namespace=namespace)
            raise

        for subset in endpoints.subsets or []:
            if subset.addresses:
                return None
        print(f"❌ Service '{service_name}' has no ready endpoints")
        return self.fail("no_ready_endpoints", service=service_name, namespace=namespace)

    def _any_app_pod_unready(self, namespace):
        pods = self.core_v1.list_namespaced_pod(namespace).items
        if not pods:
            print(f"❌ No pods found in namespace '{namespace}'")
            return self.fail("no_pods_found", namespace=namespace)

        for pod in pods:
            if pod.status.phase == "Succeeded":
                continue
            if pod.status.phase != "Running":
                print(f"❌ Pod {pod.metadata.name} is in phase: {pod.status.phase}")
                return self.fail("pods_not_ready", pod=pod.metadata.name, phase=pod.status.phase)
            for status in pod.status.container_statuses or []:
                if not status.ready:
                    print(f"❌ Container {status.name} in pod {pod.metadata.name} is not ready")
                    return self.fail("pods_not_ready", pod=pod.metadata.name, container=status.name)
        return None

    def _memory_quantity_to_kib(self, quantity):
        return int(parse_quantity(str(quantity)) / 1024)

    def _container_memory_request_kib(self, deployment):
        total = 0
        for container in deployment.spec.template.spec.containers or []:
            resources = container.resources
            if not resources or not resources.requests:
                continue
            memory = resources.requests.get("memory")
            if memory:
                total += self._memory_quantity_to_kib(memory)
        return total

    def _request_was_reduced(self, deployment, expected_memory):
        if not expected_memory:
            return None
        expected_kib = self._memory_quantity_to_kib(expected_memory)
        current_kib = self._container_memory_request_kib(deployment)
        if current_kib < expected_kib:
            print(
                f"❌ Deployment '{deployment.metadata.name}' memory request was reduced "
                f"from {expected_memory} to {current_kib}Ki"
            )
            return self.fail(
                "memory_request_reduced",
                deployment=deployment.metadata.name,
                expected=str(expected_memory),
                current_kib=current_kib,
            )
        return None

    def _target_priority_unsafe(self, deployment, platform_pc):
        name = deployment.metadata.name
        priority_name = deployment.spec.template.spec.priority_class_name
        if not priority_name:
            print(f"❌ Deployment '{name}' has no explicit priorityClassName")
            return self.fail("target_has_no_priority_class", deployment=name)

        priority_class = self._read_priority_class(priority_name)
        if priority_class is None:
            print(f"❌ Deployment '{name}' references missing PriorityClass '{priority_name}'")
            return self.fail("target_priority_class_missing", deployment=name, priority_class=priority_name)

        if (priority_class.value or 0) <= (platform_pc.value or 0):
            print(
                f"❌ Deployment '{name}' uses PriorityClass '{priority_name}' "
                f"with value {priority_class.value}, which is not higher than "
                f"'{platform_pc.metadata.name}' ({platform_pc.value})"
            )
            return self.fail(
                "target_priority_not_above_platform",
                deployment=name,
                priority_class=priority_name,
                value=priority_class.value,
                platform_value=platform_pc.value,
            )
        return None

    def evaluate(self) -> dict:
        print("== Priority Preemption Mitigation Evaluation ==")

        namespace = self.problem.namespace
        target = self.problem.faulty_service
        pressure_namespace = self.problem.PRESSURE_NAMESPACE
        pressure_deployment = self.problem.PRESSURE_DEPLOYMENT
        platform_priority = self.problem.PLATFORM_PRIORITY_CLASS

        self._wait_for_rollouts(namespace)

        target_unready, target_deployment = self._deployment_unready(target, namespace)
        if target_unready is not None:
            return target_unready

        for check in (
            self._service_endpoint_unready(target, namespace),
            self._any_deployment_unready(namespace),
            self._any_app_pod_unready(namespace),
        ):
            if check is not None:
                return check

        platform_pc = self._read_priority_class(platform_priority)
        if platform_pc is None:
            print(f"❌ PriorityClass '{platform_priority}' was deleted instead of made safe")
            return self.fail("platform_priority_class_deleted", priority_class=platform_priority)

        if platform_pc.global_default:
            print(f"❌ PriorityClass '{platform_priority}' is still an unsafe global default")
            # The injected fault, restated: this is the one check that confirms
            # the agent did not correct the priority relationship at all.
            return self.fail("platform_priority_still_global_default", priority_class=platform_priority)

        unsafe = self._target_priority_unsafe(target_deployment, platform_pc)
        if unsafe is not None:
            return unsafe

        reduced = self._request_was_reduced(target_deployment, getattr(self.problem, "target_request_memory", None))
        if reduced is not None:
            return reduced

        try:
            pressure = self.apps_v1.read_namespaced_deployment(
                name=pressure_deployment,
                namespace=pressure_namespace,
            )
        except ApiException as e:
            if e.status == 404:
                print(
                    f"❌ Pressure deployment '{pressure_namespace}/{pressure_deployment}' "
                    "was deleted instead of correcting priority policy"
                )
                # Unlike a missing app Deployment, this one is unambiguous: the
                # pressure workload is synthetic, created by the fault injector
                # and never removed by it, so its absence is the agent's doing.
                return self.fail(
                    "pressure_workload_deleted",
                    deployment=pressure_deployment,
                    namespace=pressure_namespace,
                )
            raise

        if (pressure.spec.replicas or 0) < 1:
            print(f"❌ Pressure deployment '{pressure_deployment}' was scaled to zero")
            return self.fail("pressure_workload_scaled_to_zero", deployment=pressure_deployment)

        reduced = self._request_was_reduced(pressure, getattr(self.problem, "pressure_request_memory", None))
        if reduced is not None:
            return reduced

        print("✅ App is healthy and priority policy has been corrected")
        return {"success": True}
