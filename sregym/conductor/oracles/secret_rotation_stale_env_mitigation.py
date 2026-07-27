import contextlib
import json
import logging
import shlex
import time

from kubernetes import client
from kubernetes.client.rest import ApiException

from sregym.conductor.oracles.base import Oracle

logger = logging.getLogger(__name__)


class SecretRotationStaleEnvMitigation(Oracle):
    """Evaluate whether product-catalog uses the required rotated credential."""

    importance = 1.0
    rollout_timeout_seconds = 120
    probe_timeout_seconds = 60
    poll_interval_seconds = 2
    request_timeout_seconds = 5
    frontend_service = "frontend-proxy"
    product_path = "/api/products"
    expected_product_id = "OLJCESPC7Z"

    def __init__(self, problem):
        """Capture problem constants needed to evaluate mitigation."""
        super().__init__(problem)
        self.old_conn = problem.old_conn
        self.new_conn = problem.new_conn
        self.old_password = problem.old_password
        self.new_password = problem.new_password

    def _run(self, command: str) -> str:
        """Helper to run a kubectl command for the mitigation oracle."""
        logger.debug("[secret-rotation-oracle] %s", command)
        return self.problem.kubectl.exec_command(command)

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

    def _deployment_references_secret(self, deployment: dict) -> bool:
        """Return whether product-catalog sources DB_CONNECTION_STRING from the expected Secret."""
        containers = deployment.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
        for container in containers:
            if container.get("name") != self.problem.faulty_service:
                continue
            for env in container.get("env", []):
                if env.get("name") != self.problem.secret_key:
                    continue
                secret_ref = env.get("valueFrom", {}).get("secretKeyRef", {})
                return (
                    secret_ref.get("name") == self.problem.secret_name
                    and secret_ref.get("key") == self.problem.secret_key
                )
        return False

    def _configured_connection_string(self, deployment: dict, secret_conn: str | None) -> str | None:
        """Resolve the desired product-catalog connection string from its pod template."""
        containers = deployment.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
        for container in containers:
            if container.get("name") != self.problem.faulty_service:
                continue
            for env in container.get("env", []):
                if env.get("name") != self.problem.secret_key:
                    continue
                if "value" in env:
                    return env["value"]
                secret_ref = env.get("valueFrom", {}).get("secretKeyRef", {})
                if (
                    secret_ref.get("name") == self.problem.secret_name
                    and secret_ref.get("key") == self.problem.secret_key
                ):
                    return secret_conn
                return None
        return None

    def _stale_pod_uid(self, deployment: dict) -> str | None:
        annotations = deployment.get("metadata", {}).get("annotations", {})
        return annotations.get(self.problem.SOURCE_POD_UID_ANNOTATION)

    @staticmethod
    def _pod_matches_selector(pod, selector: dict[str, str]) -> bool:
        labels = pod.metadata.labels or {}
        return all(labels.get(key) == value for key, value in selector.items())

    def _ready_target_pod_uids(self, deployment) -> tuple[bool, set[str]]:
        namespace = self.problem.namespace
        service_name = self.problem.faulty_service
        selector = deployment.spec.selector.match_labels or {}
        if not selector:
            print(f"[FAIL] Deployment '{service_name}' has no matchLabels selector")
            return False, set()

        target_pods = {
            pod.metadata.name: pod.metadata.uid
            for pod in self.problem.kubectl.list_pods(namespace).items
            if pod.metadata.deletion_timestamp is None and self._pod_matches_selector(pod, selector)
        }
        if not target_pods:
            print(f"[FAIL] Deployment '{service_name}' has no matching pods")
            return False, set()

        endpoints = self.problem.kubectl.core_v1_api.read_namespaced_endpoints(
            name=service_name,
            namespace=namespace,
        )
        ready_target_names = {
            address.target_ref.name
            for subset in endpoints.subsets or []
            for address in subset.addresses or []
            if address.target_ref is not None
            and address.target_ref.kind == "Pod"
            and address.target_ref.name in target_pods
        }
        if not ready_target_names:
            print(f"[FAIL] Service '{service_name}' has no ready endpoint from its Deployment")
            return False, set()
        return True, {target_pods[name] for name in ready_target_names}

    def _postgres_accepts_password(self, password: str | None) -> bool:
        """Return whether PostgreSQL accepts the supplied application password."""
        if not password:
            return False
        script = (
            f"if PGPASSWORD={shlex.quote(password)} psql -h {shlex.quote(self.problem.backend_service)} "
            f"-U {shlex.quote(self.problem.db_user)} -d {shlex.quote(self.problem.db_name)} -tAc 'select 1' "
            ">/dev/null 2>&1; then echo 1; else echo 0; fi"
        )
        command = (
            f"kubectl exec -n {self.problem.namespace} deploy/{self.problem.backend_service} -- "
            f"sh -lc {shlex.quote(script)}"
        )
        for attempt in range(self.problem._POSTGRES_PASSWORD_CHECK_ATTEMPTS):
            output = self._run(command)
            if output.strip() == "1":
                return True
            if attempt < self.problem._POSTGRES_PASSWORD_CHECK_ATTEMPTS - 1:
                time.sleep(self.problem._POSTGRES_PASSWORD_CHECK_INTERVAL_SECONDS)
        return False

    def _run_product_probe(self) -> bool:
        namespace = self.problem.namespace
        core_v1 = self.problem.kubectl.core_v1_api
        service = core_v1.read_namespaced_service(name=self.frontend_service, namespace=namespace)
        service_ports = service.spec.ports or []
        if not service_ports:
            print(f"[FAIL] Service '{self.frontend_service}' has no ports")
            return False

        port = service_ports[0].port
        url = f"http://{self.frontend_service}.{namespace}.svc.cluster.local:{port}{self.product_path}"
        pod_name = f"catalog-readiness-check-{time.time_ns()}"[:63]
        script = (
            "set -eu; "
            f"wget -q -T {self.request_timeout_seconds} -t 1 -O /tmp/products '{url}'; "
            f"grep -q '{self.expected_product_id}' /tmp/products; "
            "echo PRODUCTS_OK"
        )
        pod = client.V1Pod(
            metadata=client.V1ObjectMeta(
                name=pod_name,
                namespace=namespace,
                labels={"app": "catalog-readiness-check"},
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
            return phase == "Succeeded" and "PRODUCTS_OK" in logs
        except ApiException as exc:
            print(f"[FAIL] Product catalog probe failed: {exc}")
            return False
        finally:
            with contextlib.suppress(ApiException):
                core_v1.delete_namespaced_pod(
                    name=pod_name,
                    namespace=namespace,
                    grace_period_seconds=0,
                )

    def evaluate(self, *args, **kwargs) -> dict:
        """Evaluate whether the required rotation reached a fresh, functional pod."""
        print("== Secret Rotation Mitigation Evaluation ==")
        results = {
            "success": False,
            "deployment_exists": False,
            "rollout_complete": False,
            "pods_ready": False,
            "ready_target_endpoint": False,
            "stale_pod_uid": None,
            "current_pod_uids": [],
            "secret_conn": None,
            "configured_conn": None,
            "deployment_references_secret": False,
            "postgres_accepts_old_password": False,
            "postgres_accepts_new_password": False,
            "postgresql_init_uses_new_password": False,
            "product_probe_succeeded": False,
            "reason": "",
        }

        output = self._run(f"kubectl get deployment {self.problem.faulty_service} -n {self.problem.namespace} -o json")
        try:
            deployment_json = json.loads(output)
        except json.JSONDecodeError as exc:
            results["reason"] = f"product-catalog deployment does not exist: {exc}"
            return results
        try:
            deployment = self.problem.kubectl.get_deployment(
                self.problem.faulty_service,
                self.problem.namespace,
            )
        except Exception as exc:
            results["reason"] = f"product-catalog deployment does not exist: {exc}"
            return results
        results["deployment_exists"] = True

        desired = self._desired_replicas(deployment)
        if desired < 1:
            results["reason"] = f"product-catalog is scaled to {desired}"
            return results

        deployment = self._wait_for_current_rollout(deployment)
        if deployment is None:
            results["reason"] = "product-catalog did not complete its current rollout"
            return results
        results["rollout_complete"] = True
        results["pods_ready"] = True

        endpoint_ready, current_pod_uids = self._ready_target_pod_uids(deployment)
        results["ready_target_endpoint"] = endpoint_ready
        results["current_pod_uids"] = sorted(current_pod_uids)
        if not endpoint_ready:
            results["reason"] = "product-catalog has no ready endpoint from its Deployment"
            return results

        stale_pod_uid = self._stale_pod_uid(deployment_json)
        results["stale_pod_uid"] = stale_pod_uid
        if stale_pod_uid and stale_pod_uid in current_pod_uids:
            results["reason"] = "the product-catalog pod from before credential rotation is still serving"
            return results

        secret_conn = self.problem._get_secret_conn_string()
        configured_conn = self._configured_connection_string(deployment_json, secret_conn)
        results["secret_conn"] = secret_conn
        results["configured_conn"] = configured_conn
        results["deployment_references_secret"] = self._deployment_references_secret(deployment_json)
        if secret_conn != self.new_conn:
            results["reason"] = "the Secret does not contain the required rotated connection string"
            return results
        if configured_conn != self.new_conn:
            results["reason"] = "product-catalog is not configured with the required rotated connection string"
            return results

        results["postgres_accepts_old_password"] = self._postgres_accepts_password(self.old_password)
        results["postgres_accepts_new_password"] = self._postgres_accepts_password(self.new_password)
        results["postgresql_init_uses_new_password"] = self.problem._postgresql_init_uses_password(self.new_password)

        if not results["postgres_accepts_new_password"]:
            results["reason"] = "PostgreSQL does not accept the required rotated password"
            return results
        if results["postgres_accepts_old_password"]:
            results["reason"] = "PostgreSQL still accepts the pre-rotation password"
            return results
        if not results["postgresql_init_uses_new_password"]:
            results["reason"] = "postgresql-init does not declare the required rotated password"
            return results

        results["product_probe_succeeded"] = self._run_product_probe()
        if not results["product_probe_succeeded"]:
            results["reason"] = "a fresh /api/products request did not return catalog data"
            return results

        results["success"] = True
        results["reason"] = "required credential rotation is consistent and product queries succeed"
        print("Mitigation Result: Pass")
        return results
