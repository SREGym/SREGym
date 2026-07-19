import contextlib
import time

from kubernetes import client
from kubernetes.client.rest import ApiException

from sregym.conductor.oracles.base import Oracle


class NetworkPolicyMitigationOracle(Oracle):
    """Verify that the isolated workload is healthy and reachable again."""

    importance = 1.0
    rollout_timeout_seconds = 120
    probe_timeout_seconds = 60
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

    @staticmethod
    def _pod_matches_selector(pod, selector: dict[str, str]) -> bool:
        labels = pod.metadata.labels or {}
        return all(labels.get(key) == value for key, value in selector.items())

    def _service_has_ready_target_endpoint(self, deployment) -> bool:
        service_name = self.problem.faulty_service
        namespace = self.problem.namespace
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

    def _run_recommendation_probe(self) -> bool:
        namespace = self.problem.namespace
        core_v1 = self.problem.kubectl.core_v1_api
        target_service = self.problem.faulty_service
        frontend_service = getattr(self.problem.app, "frontend_service", "frontend")
        frontend_port = getattr(self.problem.app, "frontend_port", 5000)
        target = core_v1.read_namespaced_service(name=target_service, namespace=namespace)
        frontend = core_v1.read_namespaced_service(name=frontend_service, namespace=namespace)
        target_ports = target.spec.ports or []
        if not target_ports:
            print(f"[FAIL] Service '{target_service}' has no ports")
            return False

        target_port = target_ports[0].port
        source_labels = dict(frontend.spec.selector or {})
        if not source_labels:
            print(f"[FAIL] Service '{frontend_service}' has no selector for the probe's network identity")
            return False
        pod_name = f"recommendation-connectivity-check-{time.time_ns()}"[:63]
        target_dns = f"{target_service}.{namespace}.svc.cluster.local"
        url = (
            f"http://{frontend_service}.{namespace}.svc.cluster.local:{frontend_port}/recommendations"
            "?require=rate&lat=38.0235&lon=-122.095"
        )
        script = (
            f"nc -z -w 5 '{target_dns}' {target_port} && "
            f"response=$(wget -q -T 10 -O - '{url}') && "
            'printf \'%s\' "$response" | grep -q \'"type":"FeatureCollection"\' && '
            "echo RECOMMENDATION_OK"
        )
        pod = client.V1Pod(
            metadata=client.V1ObjectMeta(
                name=pod_name,
                namespace=namespace,
                labels=source_labels,
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
                        readiness_probe=client.V1Probe(
                            _exec=client.V1ExecAction(command=["sh", "-c", "exit 1"]),
                            period_seconds=1,
                            failure_threshold=1,
                        ),
                    )
                ],
            ),
        )

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
            return phase == "Succeeded" and "RECOMMENDATION_OK" in logs
        except ApiException as exc:
            print(f"[FAIL] Recommendation probe failed: {exc}")
            return False
        finally:
            with contextlib.suppress(ApiException):
                core_v1.delete_namespaced_pod(
                    name=pod_name,
                    namespace=namespace,
                    grace_period_seconds=0,
                )

    def evaluate(self) -> dict:
        print("== NetworkPolicy Mitigation Evaluation ==")

        service_name = self.problem.faulty_service
        namespace = self.problem.namespace
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

            if not self._service_has_ready_target_endpoint(deployment):
                return {"success": False}

            if not self._run_recommendation_probe():
                print("[FAIL] Hotel Reservation recommendation request did not recover")
                return {"success": False}
        except Exception as exc:
            print(f"[FAIL] Error checking NetworkPolicy mitigation: {exc}")
            return {"success": False}

        print("[PASS] Recommendation is healthy, discoverable, and reachable through the frontend")
        return {"success": True}
