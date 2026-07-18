import contextlib
import time

from kubernetes import client
from kubernetes.client.rest import ApiException

from sregym.conductor.oracles.base import Oracle


class ServiceEndpointMitigationOracle(Oracle):
    """Verify that the affected Service has current, reachable endpoints."""

    importance = 1.0
    rollout_timeout_seconds = 120
    probe_timeout_seconds = 60
    connection_timeout_seconds = 5
    poll_interval_seconds = 2

    @staticmethod
    def _pod_matches_selector(pod, selector: dict[str, str]) -> bool:
        labels = pod.metadata.labels or {}
        return all(labels.get(key) == value for key, value in selector.items())

    @staticmethod
    def _desired_replicas(deployment) -> int:
        return 1 if deployment.spec.replicas is None else deployment.spec.replicas

    @classmethod
    def _rollout_complete(cls, deployment) -> bool:
        desired = cls._desired_replicas(deployment)
        if desired < 1:
            return False
        status = deployment.status
        return (
            (status.observed_generation or 0) >= (deployment.metadata.generation or 0)
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

    def _run_connectivity_probe(self) -> bool:
        namespace = self.problem.namespace
        service_name = self.problem.faulty_service
        port = self.problem.expected_service_port
        target = f"{service_name}.{namespace}.svc.cluster.local"
        pod_name = f"service-connectivity-check-{time.time_ns()}"[:63]
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
                        command=[
                            "sh",
                            "-c",
                            f"nc -z -w {self.connection_timeout_seconds} '{target}' {port} && echo SERVICE_OK",
                        ],
                    )
                ],
            ),
        )

        core_v1 = self.problem.kubectl.core_v1_api
        try:
            core_v1.create_namespaced_pod(namespace=namespace, body=pod)
            deadline = time.monotonic() + self.probe_timeout_seconds
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

    def evaluate(self) -> dict:
        print("== Service Endpoints Evaluation ==")

        kubectl = self.problem.kubectl
        namespace = self.problem.namespace
        service_name = self.problem.faulty_service

        try:
            deployment = kubectl.get_deployment(service_name, namespace)
            if self._desired_replicas(deployment) < 1:
                print(f"❌ Deployment {service_name} is scaled to zero")
                return {"success": False}
            deployment = self._wait_for_current_rollout(deployment)
            if deployment is None:
                print(f"❌ Deployment {service_name} did not complete its current rollout")
                return {"success": False}

            deployment_selector = deployment.spec.selector.match_labels or {}
            if not deployment_selector:
                print(f"❌ Deployment {service_name} has no matchLabels selector")
                return {"success": False}

            replica_sets = kubectl.get_matching_replicasets(namespace, service_name)
            active_replica_sets = {
                replica_set.metadata.name for replica_set in replica_sets if (replica_set.spec.replicas or 0) > 0
            }
            if not active_replica_sets:
                print(f"❌ Deployment {service_name} has no active ReplicaSet")
                return {"success": False}

            expected_pods = {
                pod.metadata.name
                for pod in kubectl.list_pods(namespace).items
                if pod.metadata.deletion_timestamp is None
                and self._pod_matches_selector(pod, deployment_selector)
                and self._owned_by_active_replica_set(pod, active_replica_sets)
            }
            if not expected_pods:
                print(f"❌ Deployment {service_name} has no matching pods")
                return {"success": False}

            endpoints = kubectl.core_v1_api.read_namespaced_endpoints(service_name, namespace)
            ready_addresses = [address for subset in (endpoints.subsets or []) for address in (subset.addresses or [])]
            ready_pods = {
                address.target_ref.name
                for address in ready_addresses
                if address.target_ref is not None and address.target_ref.kind == "Pod"
            }

            if not ready_pods:
                print(f"❌ Service {service_name} has no ready pod endpoints")
                return {"success": False}

            unexpected_pods = ready_pods - expected_pods
            if unexpected_pods:
                print(f"❌ Service {service_name} selects unexpected pods: {', '.join(sorted(unexpected_pods))}")
                return {"success": False}

            if not self._run_connectivity_probe():
                print(f"❌ Service {service_name} does not accept traffic on port {self.problem.expected_service_port}")
                return {"success": False}
        except Exception as e:
            print(f"❌ Error retrieving endpoints for service {service_name}: {e}")
            return {"success": False}

        print(f"[✅] Service {service_name} has current, reachable endpoints for its intended Deployment.")
        return {"success": True}
