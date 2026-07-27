import contextlib
import time

from kubernetes import client
from kubernetes.client.rest import ApiException

from sregym.conductor.oracles.base import Oracle


class IncorrectPortAssignmentMitigationOracle(Oracle):
    importance = 1.0
    rollout_timeout_seconds = 120
    probe_timeout_seconds = 60
    poll_interval_seconds = 2
    probe_image = "fullstorydev/grpcurl:v1.9.3"
    probe_rpc = "oteldemo.ProductCatalogService/ListProducts"

    def __init__(self, problem, *, require_source_ready: bool = True):
        super().__init__(problem)
        self.require_source_ready = require_source_ready

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

    def _configured_address(self, deployment) -> str | None:
        for container in deployment.spec.template.spec.containers or []:
            for env_var in container.env or []:
                if env_var.name == self.problem.env_var:
                    return env_var.value or None
        return None

    def _has_ready_service_endpoint(self) -> bool:
        endpoints = self.problem.kubectl.core_v1_api.read_namespaced_endpoints(
            name=self.problem.faulty_service,
            namespace=self.problem.namespace,
        )
        return any(subset.addresses for subset in endpoints.subsets or [])

    def _run_dependency_probe(self, deployment, address: str) -> bool:
        namespace = self.problem.namespace
        core_v1 = self.problem.kubectl.core_v1_api
        source_template = deployment.spec.template
        source_spec = source_template.spec
        pod_name = f"service-connectivity-{time.time_ns()}"[:63]
        labels = dict(source_template.metadata.labels or {})

        pod = client.V1Pod(
            metadata=client.V1ObjectMeta(
                name=pod_name,
                namespace=namespace,
                labels=labels,
            ),
            spec=client.V1PodSpec(
                restart_policy="Never",
                active_deadline_seconds=self.probe_timeout_seconds,
                termination_grace_period_seconds=0,
                automount_service_account_token=False,
                readiness_gates=[client.V1PodReadinessGate(condition_type="operations.example.com/serving")],
                dns_policy=source_spec.dns_policy,
                dns_config=source_spec.dns_config,
                service_account_name=source_spec.service_account_name,
                image_pull_secrets=source_spec.image_pull_secrets,
                containers=[
                    client.V1Container(
                        name="connectivity-check",
                        image=self.probe_image,
                        image_pull_policy="IfNotPresent",
                        args=[
                            "-plaintext",
                            "-max-time",
                            "10",
                            "-d",
                            "{}",
                            address,
                            self.probe_rpc,
                        ],
                        resources=client.V1ResourceRequirements(
                            requests={"cpu": "5m", "memory": "16Mi"},
                            limits={"cpu": "100m", "memory": "64Mi"},
                        ),
                    )
                ],
            ),
        )

        phase = "Pending"
        logs = ""
        try:
            core_v1.create_namespaced_pod(namespace=namespace, body=pod)
            deadline = time.monotonic() + self.probe_timeout_seconds
            while True:
                current = core_v1.read_namespaced_pod(name=pod_name, namespace=namespace)
                phase = current.status.phase or "Pending"
                if phase in ("Succeeded", "Failed") or time.monotonic() >= deadline:
                    break
                time.sleep(self.poll_interval_seconds)

            if phase in ("Succeeded", "Failed"):
                logs = core_v1.read_namespaced_pod_log(name=pod_name, namespace=namespace)

            if phase != "Succeeded":
                detail = logs.strip()[-500:] or f"pod phase was {phase}"
                print(f"[FAIL] Dependency connectivity check failed: {detail}")
                return False
            return True
        except ApiException as exc:
            print(f"[FAIL] Dependency connectivity check could not run: {exc}")
            return False
        finally:
            with contextlib.suppress(ApiException):
                core_v1.delete_namespaced_pod(
                    name=pod_name,
                    namespace=namespace,
                    grace_period_seconds=0,
                )

    def evaluate(self, *args, **kwargs) -> dict:
        print("== Dependency Connectivity Evaluation ==")

        try:
            deployment = self.problem.kubectl.get_deployment(
                self.problem.faulty_service,
                self.problem.namespace,
            )
            if self.require_source_ready:
                if self._desired_replicas(deployment) < 1:
                    print(f"[FAIL] Deployment '{self.problem.faulty_service}' is scaled to zero")
                    return {"success": False}

                deployment = self._wait_for_current_rollout(deployment)
                if deployment is None:
                    print(f"[FAIL] Deployment '{self.problem.faulty_service}' did not complete its current rollout")
                    return {"success": False}

                if not self._has_ready_service_endpoint():
                    print(f"[FAIL] Service '{self.problem.faulty_service}' has no Ready endpoint")
                    return {"success": False}

            address = self._configured_address(deployment)
            if address is None:
                print(f"[FAIL] Environment variable '{self.problem.env_var}' has no literal address")
                return {"success": False}

            print(f"Checking configured dependency address '{address}'")
            if not self._run_dependency_probe(deployment, address):
                return {"success": False}
        except Exception as exc:
            print(f"[FAIL] Error checking dependency connectivity: {exc}")
            return {"success": False}

        print("[PASS] The configured dependency returned a valid product-catalog response")
        return {"success": True}
