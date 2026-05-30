"""Mitigation oracle for PriorityClass preemption cascade failures."""

import time

from kubernetes import client
from kubernetes.client.exceptions import ApiException

from sregym.conductor.oracles.base import Oracle

_ROLLOUT_SETTLE_SECONDS = 60
_ROLLOUT_POLL_INTERVAL = 5


class PriorityPreemptionMitigationOracle(Oracle):
    """Verify that app recovery came from safe priority policy, not demolition.

    A plain pod-health oracle is too weak for scheduler-priority faults: an agent
    can delete the pressure workload or remove all PriorityClasses and make the
    app look healthy without correcting the bad priority relationship. This
    oracle requires the production workload to be explicitly protected and the
    unsafe global default to be removed while keeping the synthetic tenant
    workload present.
    """

    importance = 1.0

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

    def _deployment_ready(self, name, namespace):
        try:
            deployment = self.apps_v1.read_namespaced_deployment(name=name, namespace=namespace)
        except ApiException as e:
            if e.status == 404:
                print(f"❌ Deployment '{name}' not found in namespace '{namespace}'")
                return False, None
            raise

        desired = deployment.spec.replicas or 0
        ready = deployment.status.ready_replicas or 0
        if desired < 1:
            print(f"❌ Deployment '{name}' has invalid desired replica count: {desired}")
            return False, deployment
        if ready != desired:
            print(f"❌ Deployment '{name}' has {ready}/{desired} replicas ready")
            return False, deployment
        return True, deployment

    def _service_has_ready_endpoint(self, service_name, namespace):
        try:
            endpoints = self.core_v1.read_namespaced_endpoints(service_name, namespace)
        except ApiException as e:
            if e.status == 404:
                print(f"❌ Service '{service_name}' endpoints not found in namespace '{namespace}'")
                return False
            raise

        for subset in endpoints.subsets or []:
            if subset.addresses:
                return True
        print(f"❌ Service '{service_name}' has no ready endpoints")
        return False

    def _all_app_pods_ready(self, namespace):
        pods = self.core_v1.list_namespaced_pod(namespace).items
        if not pods:
            print(f"❌ No pods found in namespace '{namespace}'")
            return False

        for pod in pods:
            if pod.status.phase == "Succeeded":
                continue
            if pod.status.phase != "Running":
                print(f"❌ Pod {pod.metadata.name} is in phase: {pod.status.phase}")
                return False
            for status in pod.status.container_statuses or []:
                if not status.ready:
                    print(f"❌ Container {status.name} in pod {pod.metadata.name} is not ready")
                    return False
        return True

    def evaluate(self) -> dict:
        print("== Priority Preemption Mitigation Evaluation ==")

        namespace = self.problem.namespace
        target = self.problem.faulty_service
        pressure_namespace = self.problem.PRESSURE_NAMESPACE
        pressure_deployment = self.problem.PRESSURE_DEPLOYMENT
        platform_priority = self.problem.PLATFORM_PRIORITY_CLASS
        production_priority = self.problem.PRODUCTION_PRIORITY_CLASS

        self._wait_for_rollouts(namespace)

        target_ready, target_deployment = self._deployment_ready(target, namespace)
        if not target_ready:
            return {"success": False}

        if not self._service_has_ready_endpoint(target, namespace):
            return {"success": False}

        if not self._all_app_pods_ready(namespace):
            return {"success": False}

        platform_pc = self._read_priority_class(platform_priority)
        production_pc = self._read_priority_class(production_priority)
        if platform_pc is None:
            print(f"❌ PriorityClass '{platform_priority}' was deleted instead of made safe")
            return {"success": False}
        if production_pc is None:
            print(f"❌ PriorityClass '{production_priority}' is missing")
            return {"success": False}

        if platform_pc.global_default:
            print(f"❌ PriorityClass '{platform_priority}' is still an unsafe global default")
            return {"success": False}
        if (production_pc.value or 0) <= (platform_pc.value or 0):
            print(
                f"❌ PriorityClass '{production_priority}' value "
                f"({production_pc.value}) is not higher than '{platform_priority}' ({platform_pc.value})"
            )
            return {"success": False}

        template_priority = target_deployment.spec.template.spec.priority_class_name
        if template_priority != production_priority:
            print(
                f"❌ Deployment '{target}' is not explicitly protected with "
                f"priorityClassName '{production_priority}'"
            )
            return {"success": False}

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
                return {"success": False}
            raise

        if (pressure.spec.replicas or 0) < 1:
            print(f"❌ Pressure deployment '{pressure_deployment}' was scaled to zero")
            return {"success": False}

        print("✅ App is healthy and priority policy has been corrected")
        return {"success": True}
