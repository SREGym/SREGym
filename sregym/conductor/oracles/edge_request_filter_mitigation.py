import contextlib
import time

from kubernetes import client
from kubernetes.client.rest import ApiException

from sregym.conductor.oracles.base import Oracle


class EdgeRequestFilterMitigationOracle(Oracle):
    """Verify that the edge filter withstands the triggering request."""

    importance = 1.0
    rollout_timeout_seconds = 120
    probe_timeout_seconds = 60
    poll_interval_seconds = 2
    request_timeout_seconds = 3

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
    def _find_container(deployment, name):
        return next(
            (container for container in deployment.spec.template.spec.containers if container.name == name),
            None,
        )

    def _known_bad_rule_active(self, container) -> bool:
        env = {item.name: item.value for item in container.env or []}

        # The injected Perl script disables filtering only for this exact value.
        if env.get("WAF_RULE_ENABLED", "true") == "false":
            return False

        configured_rule = env.get(self.problem.regex_env)
        if configured_rule == self.problem.bad_regex:
            return True

        command_text = " ".join([*(container.command or []), *(container.args or [])])
        uses_bad_fallback = self.problem.bad_regex in command_text
        return configured_rule in (None, "", "0") and uses_bad_fallback

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

    def _run_filter_probe(self) -> bool:
        namespace = self.problem.namespace
        service_name = self.problem.faulty_service
        core_v1 = self.problem.kubectl.core_v1_api
        service = core_v1.read_namespaced_service(name=service_name, namespace=namespace)
        service_ports = service.spec.ports or []
        if not service_ports:
            print(f"[FAIL] Service '{service_name}' has no ports")
            return False

        service_port = service_ports[0].port
        base_url = f"http://{service_name}.{namespace}.svc.cluster.local:{service_port}"
        crafted_url = f"{base_url}/?waf={self.problem.crafted_payload}"
        pod_name = f"frontend-filter-check-{time.time_ns()}"[:63]
        request_timeout = self.request_timeout_seconds
        script = (
            "set -eu; "
            f"wget -q -T {request_timeout} -t 1 -O /tmp/crafted '{crafted_url}'; "
            "echo CRAFTED_OK; "
            f"wget -q -T {request_timeout} -t 1 -O /tmp/normal '{base_url}/'; "
            "test -s /tmp/normal; "
            "echo NORMAL_OK"
        )
        pod = client.V1Pod(
            metadata=client.V1ObjectMeta(
                name=pod_name,
                namespace=namespace,
                labels={"app": "frontend-filter-check"},
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
            return phase == "Succeeded" and "CRAFTED_OK" in logs and "NORMAL_OK" in logs
        except ApiException as exc:
            print(f"[FAIL] Edge filter probe failed: {exc}")
            return False
        finally:
            with contextlib.suppress(ApiException):
                core_v1.delete_namespaced_pod(
                    name=pod_name,
                    namespace=namespace,
                    grace_period_seconds=0,
                )

    def evaluate(self, solution=None, trace=None, duration=None) -> dict:
        print("== Edge Request Filter Mitigation Evaluation ==")

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
                print(f"[FAIL] Deployment '{deployment_name}' did not complete its current rollout")
                return {"success": False}

            container = self._find_container(deployment, deployment_name)
            if container is None:
                print(f"[FAIL] Container '{deployment_name}' was not found")
                return {"success": False}

            if self._known_bad_rule_active(container):
                print("[FAIL] The injected vulnerable WAF rule is still active")
                return {"success": False}

            if not self._service_has_ready_target_endpoint(deployment):
                return {"success": False}

            if not self._run_filter_probe():
                print("[FAIL] Crafted and normal edge requests did not recover within the deadline")
                return {"success": False}
        except Exception as exc:
            print(f"[FAIL] Error checking edge request filter mitigation: {exc}")
            return {"success": False}

        print("[PASS] The edge accepts crafted and normal requests within the deadline")
        return {"success": True}
