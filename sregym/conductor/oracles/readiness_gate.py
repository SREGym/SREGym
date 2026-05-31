import time

from kubernetes import client
from kubernetes.client.exceptions import ApiException

from sregym.conductor.oracles.base import Oracle

_ROLLOUT_SETTLE_SECONDS = 90
_ROLLOUT_POLL_INTERVAL = 5


class ReadinessGateMitigationOracle(Oracle):
    """Oracle for faults where an orphaned Pod readiness gate keeps a deployment unavailable.

    The generic MitigationOracle is not strict enough for this class. A Pod blocked by
    a readiness gate can have phase=Running and all containers ready while the Pod
    Ready condition remains False. This oracle checks the target Deployment, Pod Ready
    condition, Service endpoints, and the absence of the injected orphan readiness gate.
    """

    importance = 1.0

    def __init__(self, problem, deployment_name: str, condition_type: str, expected_replicas: int = 1):
        super().__init__(problem)
        self.deployment_name = deployment_name
        self.condition_type = condition_type
        self.expected_replicas = expected_replicas
        self.core_api = client.CoreV1Api()

    def _wait_for_rollouts(self, kubectl, namespace: str) -> None:
        deadline = time.monotonic() + _ROLLOUT_SETTLE_SECONDS

        while time.monotonic() < deadline:
            try:
                deployments = kubectl.list_deployments(namespace)
            except Exception:
                time.sleep(_ROLLOUT_POLL_INTERVAL)
                continue

            all_settled = True
            for dep in deployments.items:
                desired = dep.spec.replicas or 0
                status = dep.status

                if desired == 0:
                    all_settled = False
                    break

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

        print("Timed out waiting for deployments to settle; evaluating current state")

    def _template_has_injected_gate(self, deployment) -> bool:
        readiness_gates = deployment.spec.template.spec.readiness_gates or []
        return any(gate.condition_type == self.condition_type for gate in readiness_gates)

    def _pod_ready_condition_true(self, pod) -> bool:
        for condition in pod.status.conditions or []:
            if condition.type == "Ready":
                return condition.status == "True"
        return False

    def _service_has_ready_endpoints(self, namespace: str, service_name: str) -> bool:
        try:
            endpoints = self.core_api.read_namespaced_endpoints(name=service_name, namespace=namespace)
        except ApiException as exc:
            if exc.status == 404:
                return False
            raise

        return any(subset.addresses for subset in endpoints.subsets or [])

    def evaluate(self) -> dict:
        print("== Readiness Gate Mitigation Evaluation ==")
        kubectl = self.problem.kubectl
        namespace = self.problem.namespace
        results = {}

        try:
            deployment = kubectl.get_deployment(self.deployment_name, namespace)
        except ApiException as exc:
            if exc.status == 404:
                print(f"Deployment '{self.deployment_name}' not found in namespace '{namespace}'")
                results["success"] = False
                return results
            raise

        desired = deployment.spec.replicas or 0

        if desired != self.expected_replicas:
            print(
                f"Deployment '{self.deployment_name}' has replicas={desired}; "
                f"expected {self.expected_replicas}. Scaling to zero is not a valid mitigation."
            )
            results["success"] = False
            return results

        if self._template_has_injected_gate(deployment):
            print(
                f"Deployment '{self.deployment_name}' still contains orphan readiness gate "
                f"'{self.condition_type}' in its pod template."
            )
            results["success"] = False
            return results

        self._wait_for_rollouts(kubectl, namespace)
        deployment = kubectl.get_deployment(self.deployment_name, namespace)

        desired = deployment.spec.replicas or 0
        ready = deployment.status.ready_replicas or 0
        available = deployment.status.available_replicas or 0

        if ready != desired or available != desired:
            print(
                f"Deployment '{self.deployment_name}' is not fully available: "
                f"ready={ready}, available={available}, desired={desired}"
            )
            results["success"] = False
            return results

        pod_list = kubectl.list_pods(namespace)
        target_pods = [
            pod
            for pod in pod_list.items
            if pod.metadata.labels and pod.metadata.labels.get("io.kompose.service") == self.deployment_name
        ]

        if not target_pods:
            print(f"No pods found for deployment '{self.deployment_name}'")
            results["success"] = False
            return results

        for pod in target_pods:
            if pod.status.phase != "Running":
                print(f"Pod {pod.metadata.name} is in phase {pod.status.phase}")
                results["success"] = False
                return results

            if not self._pod_ready_condition_true(pod):
                print(f"Pod {pod.metadata.name} Ready condition is not True")
                results["success"] = False
                return results

            for container_status in pod.status.container_statuses or []:
                if not container_status.ready:
                    print(f"Container {container_status.name} in pod {pod.metadata.name} is not ready")
                    results["success"] = False
                    return results

        if not self._service_has_ready_endpoints(namespace, self.deployment_name):
            print(f"Service '{self.deployment_name}' has no ready endpoints")
            results["success"] = False
            return results

        for pod in pod_list.items:
            if pod.status.phase != "Running":
                print(f"Pod {pod.metadata.name} is in phase {pod.status.phase}")
                results["success"] = False
                return results

            for container_status in pod.status.container_statuses or []:
                if container_status.state.waiting and container_status.state.waiting.reason:
                    print(f"Container {container_status.name} is waiting: {container_status.state.waiting.reason}")
                    results["success"] = False
                    return results

                if container_status.state.terminated and container_status.state.terminated.reason != "Completed":
                    print(f"Container {container_status.name} terminated: {container_status.state.terminated.reason}")
                    results["success"] = False
                    return results

        print(
            f"Deployment '{self.deployment_name}' has {ready}/{desired} replicas ready, "
            "service endpoints are populated, and the orphan readiness gate is gone."
        )
        results["success"] = True
        return results
