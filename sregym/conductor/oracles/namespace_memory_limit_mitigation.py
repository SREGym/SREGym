import contextlib
import time

from kubernetes import client
from kubernetes.client.rest import ApiException

from sregym.conductor.oracles.base import Oracle


class NamespaceMemoryLimitMitigationOracle(Oracle):
    """Verify that namespace-wide memory admission is safe and search recovered."""

    importance = 1.0
    rollout_timeout_seconds = 120
    probe_timeout_seconds = 60
    poll_interval_seconds = 2
    connection_timeout_seconds = 3
    memory_quota_keys = frozenset({"memory", "requests.memory", "limits.memory"})

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
            and (status.replicas or 0) == desired
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

    def _active_memory_quotas(self) -> list[str]:
        return [
            quota.metadata.name
            for quota in self.problem.kubectl.get_resource_quotas(self.problem.namespace)
            if set(quota.spec.hard or {}) & self.memory_quota_keys
        ]

    @staticmethod
    def _pod_matches_selector(pod, selector: dict[str, str]) -> bool:
        labels = pod.metadata.labels or {}
        return all(labels.get(key) == value for key, value in selector.items())

    def _service_has_ready_target_endpoint(self, deployment) -> bool:
        namespace = self.problem.namespace
        service_name = self.problem.faulty_service
        selector = deployment.spec.selector.match_labels or {}
        if not selector:
            print(f"[FAIL] Deployment '{service_name}' has no matchLabels selector")
            return False

        target_pods = {
            pod.metadata.name
            for pod in self.problem.kubectl.list_pods(namespace).items
            if self._pod_matches_selector(pod, selector)
        }
        if not target_pods:
            print(f"[FAIL] Deployment '{service_name}' has no matching pods")
            return False

        endpoints = self.problem.kubectl.core_v1_api.read_namespaced_endpoints(
            name=service_name,
            namespace=namespace,
        )
        ready_target_pods = {
            address.target_ref.name
            for subset in endpoints.subsets or []
            for address in subset.addresses or []
            if address.target_ref is not None
            and address.target_ref.kind == "Pod"
            and address.target_ref.name in target_pods
        }
        if not ready_target_pods:
            print(f"[FAIL] Service '{service_name}' has no ready endpoint from its Deployment")
            return False
        return True

    def _run_fresh_admission_and_connection_probe(self) -> bool:
        namespace = self.problem.namespace
        service_name = self.problem.faulty_service
        core_v1 = self.problem.kubectl.core_v1_api
        service = core_v1.read_namespaced_service(name=service_name, namespace=namespace)
        service_ports = service.spec.ports or []
        if not service_ports:
            print(f"[FAIL] Service '{service_name}' has no ports")
            return False

        service_port = service_ports[0].port
        target = f"{service_name}.{namespace}.svc.cluster.local"
        pod_name = f"search-admission-check-{time.time_ns()}"[:63]
        script = f"nc -z -w {self.connection_timeout_seconds} '{target}' {service_port} && echo SEARCH_OK"
        pod = client.V1Pod(
            metadata=client.V1ObjectMeta(
                name=pod_name,
                namespace=namespace,
                labels={"app": "search-admission-check"},
            ),
            spec=client.V1PodSpec(
                restart_policy="Never",
                automount_service_account_token=False,
                containers=[
                    client.V1Container(
                        name="probe",
                        image="busybox:1.36",
                        image_pull_policy="IfNotPresent",
                        command=["sh", "-c", script],
                    )
                ],
            ),
        )

        try:
            # The probe intentionally has no memory declaration. Admission proves
            # the injected namespace-wide requirement is no longer effective.
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
            return phase == "Succeeded" and "SEARCH_OK" in logs
        except ApiException as exc:
            print(f"[FAIL] Fresh pod admission or search connection failed: {exc}")
            return False
        finally:
            with contextlib.suppress(ApiException):
                core_v1.delete_namespaced_pod(
                    name=pod_name,
                    namespace=namespace,
                    grace_period_seconds=0,
                )

    def evaluate(self) -> dict:
        print("== Namespace Memory Limit Mitigation Evaluation ==")

        namespace = self.problem.namespace
        deployment_name = self.problem.faulty_service
        try:
            active_memory_quotas = self._active_memory_quotas()
            if active_memory_quotas:
                names = ", ".join(sorted(active_memory_quotas))
                print(f"[FAIL] Namespace-wide memory admission requirements remain: {names}")
                return {"success": False}

            deployment = self.problem.kubectl.get_deployment(deployment_name, namespace)
            desired = self._desired_replicas(deployment)
            if desired < 1:
                print(f"[FAIL] Deployment '{deployment_name}' is scaled to {desired}")
                return {"success": False}

            deployment = self._wait_for_current_rollout(deployment)
            if deployment is None:
                print(f"[FAIL] Deployment '{deployment_name}' did not complete its current rollout")
                return {"success": False}

            if not self._service_has_ready_target_endpoint(deployment):
                return {"success": False}

            if not self._run_fresh_admission_and_connection_probe():
                print("[FAIL] A fresh no-memory-resource pod could not connect to search")
                return {"success": False}
        except Exception as exc:
            print(f"[FAIL] Error checking namespace memory mitigation: {exc}")
            return {"success": False}

        print("[PASS] Namespace memory admission is safe and search is reachable")
        return {"success": True}
