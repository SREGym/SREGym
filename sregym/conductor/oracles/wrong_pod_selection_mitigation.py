"""Mitigation oracle for frontend Service endpoint pollution."""

import contextlib
import time

from kubernetes import client
from kubernetes.client.rest import ApiException

from sregym.conductor.oracles.base import Oracle


class WrongPodSelectionMitigationOracle(Oracle):
    """Verify endpoint identity, affected workload health, and Service connectivity."""

    importance = 1.0
    rollout_timeout_seconds = 120
    probe_timeout_seconds = 60
    poll_interval_seconds = 2

    def __init__(self, problem):
        super().__init__(problem)
        self.discovery_v1 = client.DiscoveryV1Api()

    @staticmethod
    def _rollout_complete(deployment) -> bool:
        desired = deployment.spec.replicas
        if desired is None:
            desired = 1
        if desired < 1:
            return False

        status = deployment.status
        generation = deployment.metadata.generation or 0
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

    def _required_deployments_healthy(self) -> bool:
        for name in (self.problem.frontend_service, self.problem.wrong_deployment):
            try:
                deployment = self.problem.kubectl.get_deployment(name, self.problem.namespace)
            except ApiException as exc:
                if exc.status == 404:
                    print(f"Required Deployment {name} is missing.")
                    return False
                raise

            if deployment.spec.replicas is not None and deployment.spec.replicas < 1:
                print(f"Required Deployment {name} is scaled to zero.")
                return False

            if self._wait_for_current_rollout(deployment) is None:
                print(f"Required Deployment {name} is not fully rolled out and Ready.")
                return False
        return True

    def _active_replica_sets(self, deployment_name: str) -> set[str]:
        replica_sets = self.problem.kubectl.get_matching_replicasets(
            self.problem.namespace,
            deployment_name,
        )
        return {replica_set.metadata.name for replica_set in replica_sets if (replica_set.spec.replicas or 0) > 0}

    @staticmethod
    def _owned_by_replica_set(pod, replica_sets: set[str]) -> bool:
        return any(
            owner.kind == "ReplicaSet" and owner.name in replica_sets for owner in pod.metadata.owner_references or []
        )

    def _run_connectivity_probe(self) -> bool:
        namespace = self.problem.namespace
        service_name = self.problem.frontend_service
        port = self.problem.expected_service_port
        dns_name = f"{service_name}.{namespace}.svc.cluster.local"
        pod_name = f"frontend-connectivity-check-{time.time_ns()}"[:63]
        pod = client.V1Pod(
            metadata=client.V1ObjectMeta(
                name=pod_name,
                namespace=namespace,
                labels={"app": "frontend-connectivity-check"},
            ),
            spec=client.V1PodSpec(
                restart_policy="Never",
                automount_service_account_token=False,
                containers=[
                    client.V1Container(
                        name="probe",
                        image="busybox:1.36",
                        image_pull_policy="IfNotPresent",
                        command=[
                            "sh",
                            "-c",
                            f"nc -z -w 5 {dns_name} {port} && echo SERVICE_OK",
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
            print(f"Frontend connectivity probe failed: {exc}")
            return False
        finally:
            with contextlib.suppress(ApiException):
                core_v1.delete_namespaced_pod(name=pod_name, namespace=namespace, grace_period_seconds=0)

    def evaluate(self) -> dict:
        print("== Wrong Pod Selection Mitigation Evaluation ==")

        kubectl = self.problem.kubectl
        namespace = self.problem.namespace
        service_name = self.problem.frontend_service
        expected_pod_label = self.problem.expected_endpoint_pod_label

        if not self._required_deployments_healthy():
            return {"success": False}

        selected_pods = self._endpoint_pod_names(kubectl, namespace, service_name)
        if not selected_pods:
            print(f"Service {service_name} has no Ready endpoint pods")
            return {"success": False}

        active_frontend_replica_sets = self._active_replica_sets(service_name)
        if not active_frontend_replica_sets:
            print(f"Deployment {service_name} has no active ReplicaSet.")
            return {"success": False}

        wrong_pods = []
        for pod_name in selected_pods:
            pod = kubectl.core_v1_api.read_namespaced_pod(pod_name, namespace)
            labels = pod.metadata.labels or {}
            if (
                pod.metadata.deletion_timestamp is not None
                or labels.get("io.kompose.service") != expected_pod_label
                or not self._owned_by_replica_set(pod, active_frontend_replica_sets)
            ):
                wrong_pods.append(pod_name)

        if wrong_pods:
            print(f"Service {service_name} still selects non-frontend endpoint pods: {wrong_pods}")
            return {"success": False}

        if not self._run_connectivity_probe():
            print(
                f"Service {service_name} does not accept TCP traffic on expected port "
                f"{self.problem.expected_service_port}."
            )
            return {"success": False}

        print(
            f"Service {service_name} selects only frontend endpoints and accepts traffic on "
            f"port {self.problem.expected_service_port}."
        )
        return {"success": True}

    def _endpoint_pod_names(self, kubectl, namespace: str, service_name: str) -> set[str]:
        try:
            endpoint_slices = self.discovery_v1.list_namespaced_endpoint_slice(
                namespace=namespace,
                label_selector=f"kubernetes.io/service-name={service_name}",
            )
            pod_names = set()
            for endpoint_slice in endpoint_slices.items:
                for endpoint in endpoint_slice.endpoints or []:
                    ready = endpoint.conditions.ready if endpoint.conditions else None
                    if endpoint.target_ref and endpoint.target_ref.kind == "Pod" and ready is True:
                        pod_names.add(endpoint.target_ref.name)
            if pod_names:
                return pod_names
            print("EndpointSlice lookup returned no Ready pod targetRefs, falling back to Endpoints API.")
        except ApiException as exc:
            print(f"EndpointSlice lookup failed, falling back to Endpoints API: {exc}")

        endpoints = kubectl.core_v1_api.read_namespaced_endpoints(service_name, namespace)
        pod_names = set()
        for subset in endpoints.subsets or []:
            for address in subset.addresses or []:
                if address.target_ref and address.target_ref.kind == "Pod":
                    pod_names.add(address.target_ref.name)
        return pod_names
