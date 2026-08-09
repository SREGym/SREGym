import contextlib
import time

from kubernetes import client
from kubernetes.client.rest import ApiException

from sregym.conductor.oracles.base import Oracle


class ReadinessProbeMitigationOracle(Oracle):
    """Verify that the affected Deployment has current, reachable endpoints."""

    importance = 1.0
    rollout_timeout_seconds = 120
    connection_timeout_seconds = 3
    check_timeout_seconds = 60
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
    def _owned_by_active_replica_set(pod, active_replica_sets: set[str]) -> bool:
        return any(
            owner.kind == "ReplicaSet" and owner.name in active_replica_sets
            for owner in pod.metadata.owner_references or []
        )

    def _ready_current_endpoint_pods(self) -> set[str]:
        namespace = self.problem.namespace
        service_name = self.problem.faulty_service
        replica_sets = self.problem.kubectl.get_matching_replicasets(namespace, service_name)
        active_replica_sets = {
            replica_set.metadata.name for replica_set in replica_sets if (replica_set.spec.replicas or 0) > 0
        }
        if not active_replica_sets:
            print(f"[FAIL] Deployment '{service_name}' has no active ReplicaSet")
            return set()

        current_pods = {
            pod.metadata.name
            for pod in self.problem.kubectl.list_pods(namespace).items
            if pod.metadata.deletion_timestamp is None and self._owned_by_active_replica_set(pod, active_replica_sets)
        }
        if not current_pods:
            print(f"[FAIL] Deployment '{service_name}' has no current pods")
            return set()

        endpoints = self.problem.kubectl.core_v1_api.read_namespaced_endpoints(
            name=service_name,
            namespace=namespace,
        )
        ready_current_pods = {
            address.target_ref.name
            for subset in endpoints.subsets or []
            for address in subset.addresses or []
            if address.target_ref is not None
            and address.target_ref.kind == "Pod"
            and address.target_ref.name in current_pods
        }
        if not ready_current_pods:
            print(f"[FAIL] Service '{service_name}' has no ready endpoint from its current ReplicaSet")
        return ready_current_pods

    def _run_connectivity_check(self) -> bool:
        namespace = self.problem.namespace
        service_name = self.problem.faulty_service
        core_v1 = self.problem.kubectl.core_v1_api
        service = core_v1.read_namespaced_service(name=service_name, namespace=namespace)
        ports = service.spec.ports or []
        if not ports:
            print(f"[FAIL] Service '{service_name}' has no ports")
            return False

        port = ports[0].port
        target = f"{service_name}.{namespace}.svc.cluster.local"
        pod_name = f"service-connectivity-check-{time.time_ns()}"[:63]
        script = f"nc -z -w {self.connection_timeout_seconds} '{target}' {port} && echo SERVICE_OK"
        pod = client.V1Pod(
            metadata=client.V1ObjectMeta(
                name=pod_name,
                namespace=namespace,
                labels={"app": "service-connectivity-check"},
            ),
            spec=client.V1PodSpec(
                restart_policy="Never",
                automount_service_account_token=False,
                containers=[
                    client.V1Container(
                        name="check",
                        image="busybox:1.36",
                        image_pull_policy="IfNotPresent",
                        command=["sh", "-c", script],
                    )
                ],
            ),
        )

        try:
            core_v1.create_namespaced_pod(namespace=namespace, body=pod)
            deadline = time.monotonic() + self.check_timeout_seconds
            phase = "Pending"
            while time.monotonic() < deadline:
                current = core_v1.read_namespaced_pod(name=pod_name, namespace=namespace)
                phase = current.status.phase or "Pending"
                if phase in ("Succeeded", "Failed"):
                    break
                time.sleep(self.poll_interval_seconds)

            logs = core_v1.read_namespaced_pod_log(name=pod_name, namespace=namespace)
            print(logs.strip())
            return phase == "Succeeded" and "SERVICE_OK" in logs
        except ApiException as exc:
            print(f"[FAIL] Service connectivity check failed: {exc}")
            return False
        finally:
            with contextlib.suppress(ApiException):
                core_v1.delete_namespaced_pod(
                    name=pod_name,
                    namespace=namespace,
                    grace_period_seconds=0,
                )

    def evaluate(self, *args, **kwargs) -> dict:
        print("== Readiness Probe Mitigation Evaluation ==")

        namespace = self.problem.namespace
        service_name = self.problem.faulty_service
        try:
            deployment = self.problem.kubectl.get_deployment(service_name, namespace)
            desired = self._desired_replicas(deployment)
            if desired < 1:
                print(f"[FAIL] Deployment '{service_name}' is scaled to {desired}")
                return {"success": False}

            deployment = self._wait_for_current_rollout(deployment)
            if deployment is None:
                print(f"[FAIL] Deployment '{service_name}' did not complete its current rollout")
                return {"success": False}

            if not self._ready_current_endpoint_pods():
                return {"success": False}

            if not self._run_connectivity_check():
                print(f"[FAIL] A fresh connection to Service '{service_name}' did not succeed")
                return {"success": False}
        except Exception as exc:
            print(f"[FAIL] Error checking readiness recovery: {exc}")
            return {"success": False}

        print(f"[PASS] Deployment '{service_name}' has current, reachable endpoints")
        return {"success": True}
