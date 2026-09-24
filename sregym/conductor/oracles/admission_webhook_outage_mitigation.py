import time

from kubernetes import client

from sregym.conductor.oracles.base import Oracle
from sregym.service.rollout import deployment_rollout_complete


class AdmissionWebhookOutageMitigationOracle(Oracle):
    """Prove that the target Deployment can durably recreate a serving pod."""

    importance = 1.0
    rollout_timeout_seconds = 120
    replacement_timeout_seconds = 120
    poll_interval_seconds = 2

    @staticmethod
    def _desired_replicas(deployment) -> int:
        replicas = deployment.spec.replicas
        return 1 if replicas is None else replicas

    @classmethod
    def _rollout_complete(cls, deployment) -> bool:
        return deployment_rollout_complete(deployment)

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
        namespace = self.problem.namespace
        deployment_name = self.problem.faulty_service
        replica_sets = self.problem.kubectl.get_matching_replicasets(namespace, deployment_name)
        return {replica_set.metadata.name for replica_set in replica_sets if (replica_set.spec.replicas or 0) > 0}

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

    def _current_ready_pods(self) -> list:
        active_replica_sets = self._active_replica_sets()
        if not active_replica_sets:
            return []

        return [
            pod
            for pod in self.problem.kubectl.list_pods(self.problem.namespace).items
            if pod.metadata.deletion_timestamp is None
            and self._owned_by_active_replica_set(pod, active_replica_sets)
            and self._pod_ready(pod)
        ]

    def _current_ready_endpoint_pods(self) -> list:
        endpoint_names = self._ready_endpoint_names()
        return [pod for pod in self._current_ready_pods() if pod.metadata.name in endpoint_names]

    def _wait_for_new_ready_endpoint(self, previous_uids: set[str]) -> bool:
        deadline = time.monotonic() + self.replacement_timeout_seconds
        while True:
            deployment = self.problem.kubectl.get_deployment(
                self.problem.faulty_service,
                self.problem.namespace,
            )
            if self._desired_replicas(deployment) < 1:
                return False

            if self._rollout_complete(deployment):
                replacement_pods = [
                    pod for pod in self._current_ready_endpoint_pods() if str(pod.metadata.uid) not in previous_uids
                ]
                if replacement_pods:
                    replacement = sorted(replacement_pods, key=lambda pod: pod.metadata.name)[0]
                    print(f"Replacement pod '{replacement.metadata.name}' is Ready and serving the target Service")
                    return True

            if time.monotonic() >= deadline:
                return False
            time.sleep(self.poll_interval_seconds)

    def evaluate(self, *args, **kwargs) -> dict:
        print("== Pod Recreation Durability Evaluation ==")

        namespace = self.problem.namespace
        deployment_name = self.problem.faulty_service
        try:
            deployment = self.problem.kubectl.get_deployment(deployment_name, namespace)
            desired = self._desired_replicas(deployment)
            if desired < 1:
                print(f"[FAIL] Deployment '{deployment_name}' is scaled to {desired}")
                return self.fail("required_deployment_scaled_to_zero", deployment=deployment_name, desired=desired)

            deployment = self._wait_for_current_rollout(deployment)
            if deployment is None:
                print(f"[FAIL] Deployment '{deployment_name}' is not currently fully Ready")
                return self.fail("required_deployment_not_rolled_out", deployment=deployment_name)

            current_pods = self._current_ready_endpoint_pods()
            if not current_pods:
                print(f"[FAIL] Deployment '{deployment_name}' has no current Ready Service endpoint")
                return self.fail("no_ready_endpoints", deployment=deployment_name)

            previous_uids = {str(pod.metadata.uid) for pod in self._current_ready_pods()}
            target = sorted(current_pods, key=lambda pod: pod.metadata.name)[0]
            self.problem.kubectl.core_v1_api.delete_namespaced_pod(
                name=target.metadata.name,
                namespace=namespace,
                body=client.V1DeleteOptions(grace_period_seconds=0),
            )
            print(f"Deleted current pod '{target.metadata.name}' to verify replacement admission")

            if not self._wait_for_new_ready_endpoint(previous_uids):
                print(f"[FAIL] Deployment '{deployment_name}' could not create a new serving pod")
                # The oracle deletes a live pod on purpose: a replacement that
                # never gets admitted means the webhook outage still blocks
                # admission, which is the injected fault.
                return self.fail("fault_still_present", deployment=deployment_name)
        except Exception as exc:
            print(f"[FAIL] Error checking pod recreation durability: {exc}")
            return self.fail_from_exception(exc)

        print(f"[PASS] Deployment '{deployment_name}' recreated a new serving pod")
        return {"success": True}
