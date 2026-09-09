import contextlib
import time

from kubernetes import client
from kubernetes.client.rest import ApiException

from sregym.conductor.oracles.base import Oracle
from sregym.conductor.oracles.failure import FailureClass


class ServiceEndpointMitigationOracle(Oracle):
    """Verify that the affected Service has current, reachable endpoints."""

    importance = 1.0

    # Two local reasons, both spec edits rather than symptoms:
    # ``unexpected_pods_selected`` is this problem's injected fault restated,
    # and a Deployment with no matchLabels cannot have arrived that way from
    # the chart -- someone removed it.
    FAILURE_CLASSES = {
        "unexpected_pods_selected": FailureClass.AGENT_ERROR,
        "deployment_selector_missing": FailureClass.AGENT_ERROR,
    }
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
                return self.fail("required_deployment_scaled_to_zero", deployment=service_name)
            deployment = self._wait_for_current_rollout(deployment)
            if deployment is None:
                print(f"❌ Deployment {service_name} did not complete its current rollout")
                return self.fail(
                    "required_deployment_not_rolled_out",
                    deployment=service_name,
                    waited_seconds=self.rollout_timeout_seconds,
                )

            deployment_selector = deployment.spec.selector.match_labels or {}
            if not deployment_selector:
                print(f"❌ Deployment {service_name} has no matchLabels selector")
                return self.fail("deployment_selector_missing", deployment=service_name)

            replica_sets = kubectl.get_matching_replicasets(namespace, service_name)
            active_replica_sets = {
                replica_set.metadata.name for replica_set in replica_sets if (replica_set.spec.replicas or 0) > 0
            }
            if not active_replica_sets:
                print(f"❌ Deployment {service_name} has no active ReplicaSet")
                return self.fail("no_active_replicaset", deployment=service_name)

            expected_pods = {
                pod.metadata.name
                for pod in kubectl.list_pods(namespace).items
                if pod.metadata.deletion_timestamp is None
                and self._pod_matches_selector(pod, deployment_selector)
                and self._owned_by_active_replica_set(pod, active_replica_sets)
            }
            if not expected_pods:
                print(f"❌ Deployment {service_name} has no matching pods")
                return self.fail("no_matching_pods", deployment=service_name, selector=deployment_selector)

            endpoints = kubectl.core_v1_api.read_namespaced_endpoints(service_name, namespace)
            ready_addresses = [address for subset in (endpoints.subsets or []) for address in (subset.addresses or [])]
            ready_pods = {
                address.target_ref.name
                for address in ready_addresses
                if address.target_ref is not None and address.target_ref.kind == "Pod"
            }

            if not ready_pods:
                print(f"❌ Service {service_name} has no ready pod endpoints")
                return self.fail("no_ready_endpoints", service=service_name)

            unexpected_pods = ready_pods - expected_pods
            if unexpected_pods:
                print(f"❌ Service {service_name} selects unexpected pods: {', '.join(sorted(unexpected_pods))}")
                # The injected fault restated: the Service still routes to pods
                # that are not its Deployment's.
                return self.fail("unexpected_pods_selected", service=service_name, pods=sorted(unexpected_pods))

            if not self._run_connectivity_probe():
                print(f"❌ Service {service_name} does not accept traffic on port {self.problem.expected_service_port}")
                return self.fail(
                    "connectivity_probe_failed",
                    service=service_name,
                    port=self.problem.expected_service_port,
                )
        except Exception as e:
            print(f"❌ Error retrieving endpoints for service {service_name}: {e}")
            # An ApiException here is the cluster refusing to answer, not a bug
            # in this oracle. Collapsing both into one bare failure blamed us
            # for the former and hid the latter.
            return self.fail_from_exception(e, service=service_name)

        print(f"[✅] Service {service_name} has current, reachable endpoints for its intended Deployment.")
        return {"success": True}
