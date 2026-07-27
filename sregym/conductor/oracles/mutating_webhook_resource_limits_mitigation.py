import time

from kubernetes import client
from kubernetes.utils.quantity import parse_quantity

from sregym.conductor.oracles.base import Oracle


class MutatingWebhookResourceLimitsMitigationOracle(Oracle):
    """Verify that a recreated target pod keeps its intended memory resources."""

    importance = 1.0
    rollout_timeout_seconds = 120
    replacement_timeout_seconds = 120
    stability_seconds = 15
    poll_interval_seconds = 2

    @staticmethod
    def _desired_replicas(deployment) -> int:
        replicas = deployment.spec.replicas
        return 1 if replicas is None else replicas

    @classmethod
    def _rollout_complete(cls, deployment) -> bool:
        desired = cls._desired_replicas(deployment)
        if desired < 1:
            return False

        generation = deployment.metadata.generation or 0
        status = deployment.status
        return (
            (status.observed_generation or 0) >= generation
            and (status.updated_replicas or 0) == desired
            and (status.ready_replicas or 0) == desired
            and (status.available_replicas or 0) == desired
            and (status.unavailable_replicas or 0) == 0
        )

    def _wait_for_current_rollout(self, deployment):
        deadline = time.monotonic() + self.rollout_timeout_seconds
        while True:
            if self._rollout_complete(deployment):
                return deployment
            if time.monotonic() >= deadline:
                return None

            time.sleep(self.poll_interval_seconds)
            deployment = self.problem.kubectl.get_deployment(
                deployment.metadata.name,
                self.problem.namespace,
            )

    @staticmethod
    def _pod_ready(pod) -> bool:
        statuses = pod.status.container_statuses or []
        return pod.status.phase == "Running" and bool(statuses) and all(status.ready for status in statuses)

    @staticmethod
    def _owned_by_active_replica_set(pod, active_replica_sets: set[str]) -> bool:
        return any(
            owner.kind == "ReplicaSet" and owner.name in active_replica_sets
            for owner in pod.metadata.owner_references or []
        )

    def _active_replica_sets(self) -> set[str]:
        replica_sets = self.problem.kubectl.get_matching_replicasets(
            self.problem.namespace,
            self.problem.faulty_service,
        )
        return {replica_set.metadata.name for replica_set in replica_sets if (replica_set.spec.replicas or 0) > 0}

    def _current_target_pods(self) -> list:
        active_replica_sets = self._active_replica_sets()
        if not active_replica_sets:
            return []
        return [
            pod
            for pod in self.problem.kubectl.list_pods(self.problem.namespace).items
            if pod.metadata.deletion_timestamp is None and self._owned_by_active_replica_set(pod, active_replica_sets)
        ]

    def _ready_endpoint_names(self) -> set[str]:
        endpoints = self.problem.kubectl.core_v1_api.read_namespaced_endpoints(
            name=self.problem.faulty_service,
            namespace=self.problem.namespace,
        )
        return {
            address.target_ref.name
            for subset in endpoints.subsets or []
            for address in subset.addresses or []
            if address.target_ref is not None and address.target_ref.kind == "Pod"
        }

    def _current_ready_endpoint_pods(self) -> list:
        endpoint_names = self._ready_endpoint_names()
        return [
            pod for pod in self._current_target_pods() if pod.metadata.name in endpoint_names and self._pod_ready(pod)
        ]

    @staticmethod
    def _memory_resources(container) -> tuple:
        resources = container.resources
        requests = resources.requests if resources and resources.requests else {}
        limits = resources.limits if resources and resources.limits else {}

        def normalize(value):
            return None if value is None else parse_quantity(str(value))

        return normalize(requests.get("memory")), normalize(limits.get("memory"))

    @classmethod
    def _intended_memory(cls, deployment) -> tuple[str, tuple]:
        containers = deployment.spec.template.spec.containers or []
        if not containers:
            raise RuntimeError("Target Deployment has no containers")
        container = containers[0]
        return container.name, cls._memory_resources(container)

    @classmethod
    def _pod_memory(cls, pod, container_name: str) -> tuple:
        container = next(
            (container for container in pod.spec.containers or [] if container.name == container_name),
            None,
        )
        if container is None:
            raise RuntimeError(f"Replacement pod is missing container '{container_name}'")
        return cls._memory_resources(container)

    def _wait_for_replacement(
        self,
        previous_uids: set[str],
        container_name: str,
        intended_memory: tuple,
    ):
        deadline = time.monotonic() + self.replacement_timeout_seconds
        while True:
            deployment = self.problem.kubectl.get_deployment(
                self.problem.faulty_service,
                self.problem.namespace,
            )
            if self._desired_replicas(deployment) < 1:
                return None

            replacement_pods = [
                pod for pod in self._current_target_pods() if str(pod.metadata.uid) not in previous_uids
            ]
            for pod in replacement_pods:
                actual_memory = self._pod_memory(pod, container_name)
                if actual_memory != intended_memory:
                    print(
                        f"[FAIL] Replacement pod '{pod.metadata.name}' memory {actual_memory} "
                        f"does not match Deployment template {intended_memory}"
                    )
                    return None

            endpoint_names = self._ready_endpoint_names()
            ready_replacements = [
                pod for pod in replacement_pods if pod.metadata.name in endpoint_names and self._pod_ready(pod)
            ]
            if self._rollout_complete(deployment) and ready_replacements:
                return sorted(ready_replacements, key=lambda pod: pod.metadata.name)[0]

            if time.monotonic() >= deadline:
                return None
            time.sleep(self.poll_interval_seconds)

    @staticmethod
    def _oomkilled(container_status) -> bool:
        state = container_status.state
        last_state = container_status.last_state
        return bool(
            (state and state.terminated and state.terminated.reason == "OOMKilled")
            or (last_state and last_state.terminated and last_state.terminated.reason == "OOMKilled")
        )

    def _replacement_stable(self, pod, container_name: str) -> bool:
        initial_status = next(status for status in pod.status.container_statuses or [] if status.name == container_name)
        initial_restarts = initial_status.restart_count or 0
        deadline = time.monotonic() + self.stability_seconds

        while True:
            current = self.problem.kubectl.core_v1_api.read_namespaced_pod(
                name=pod.metadata.name,
                namespace=self.problem.namespace,
            )
            if str(current.metadata.uid) != str(pod.metadata.uid) or not self._pod_ready(current):
                return False

            status = next(
                (item for item in current.status.container_statuses or [] if item.name == container_name),
                None,
            )
            if status is None or (status.restart_count or 0) > initial_restarts or self._oomkilled(status):
                return False

            if pod.metadata.name not in self._ready_endpoint_names():
                return False
            if time.monotonic() >= deadline:
                return True
            time.sleep(self.poll_interval_seconds)

    def evaluate(self, *args, **kwargs) -> dict:
        print("== Pod Resource Durability Evaluation ==")

        namespace = self.problem.namespace
        deployment_name = self.problem.faulty_service
        try:
            deployment = self.problem.kubectl.get_deployment(deployment_name, namespace)
            desired = self._desired_replicas(deployment)
            if desired < 1:
                print(f"[FAIL] Deployment '{deployment_name}' is scaled to {desired}")
                return {"success": False}

            deployment = self._wait_for_current_rollout(deployment)
            if deployment is None:
                print(f"[FAIL] Deployment '{deployment_name}' is not currently fully Ready")
                return {"success": False}

            current_pods = self._current_ready_endpoint_pods()
            if not current_pods:
                print(f"[FAIL] Deployment '{deployment_name}' has no current Ready Service endpoint")
                return {"success": False}

            container_name, intended_memory = self._intended_memory(deployment)
            previous_uids = {str(pod.metadata.uid) for pod in self._current_target_pods()}
            target = sorted(current_pods, key=lambda pod: pod.metadata.name)[0]
            self.problem.kubectl.core_v1_api.delete_namespaced_pod(
                name=target.metadata.name,
                namespace=namespace,
                body=client.V1DeleteOptions(grace_period_seconds=0),
            )
            print(f"Deleted current pod '{target.metadata.name}' to verify admission-time resources")

            replacement = self._wait_for_replacement(
                previous_uids,
                container_name,
                intended_memory,
            )
            if replacement is None:
                print(f"[FAIL] Deployment '{deployment_name}' did not create a correct serving replacement")
                return {"success": False}

            if not self._replacement_stable(replacement, container_name):
                print(f"[FAIL] Replacement pod '{replacement.metadata.name}' did not remain stable")
                return {"success": False}
        except Exception as exc:
            print(f"[FAIL] Error checking pod resource recovery: {exc}")
            return {"success": False}

        print(f"[PASS] Deployment '{deployment_name}' recreated a stable pod with intended resources")
        return {"success": True}
