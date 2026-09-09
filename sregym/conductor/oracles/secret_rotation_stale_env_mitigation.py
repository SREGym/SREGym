import contextlib
import json
import logging
import shlex
import time

from kubernetes import client
from kubernetes.client.rest import ApiException

from sregym.conductor.oracles.base import Oracle
from sregym.conductor.oracles.failure import FailureClass

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
        return self.problem.kubectl.exec_command_checked(command, timeout=30)

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

    def _postgres_accepts_password(self, password: str | None) -> bool | None:
        """Return acceptance, confirmed rejection, or None for an unreadable response."""
        if not password:
            return False
        rejection = shlex.quote(f'password authentication failed for user "{self.problem.db_user}"')
        script = (
            f"if output=$(LC_ALL=C PGCONNECT_TIMEOUT=5 PGPASSWORD={shlex.quote(password)} "
            f"psql -X -w -h {shlex.quote(self.problem.backend_service)} "
            f"-U {shlex.quote(self.problem.db_user)} -d {shlex.quote(self.problem.db_name)} -tAc 'select 1' 2>&1); "
            'then printf "%s\\n" "$output"; else status=$?; '
            # Only a PostgreSQL authentication rejection is a negative password result.
            # Other psql errors keep their exit status through kubectl exec.
            f'case "$output" in *FATAL:*{rejection}*) echo PASSWORD_REJECTED;; '
            '*) printf "%s\\n" "$output" >&2; exit "$status";; esac; fi'
        )
        command = (
            f"kubectl exec -n {self.problem.namespace} deploy/{self.problem.backend_service} -- "
            f"sh -lc {shlex.quote(script)}"
        )
        for attempt in range(self.problem._POSTGRES_PASSWORD_CHECK_ATTEMPTS):
            output = self._run(command)
            if output.strip() == "1":
                return True
            if output.strip() != "PASSWORD_REJECTED":
                return None
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
            raise
        finally:
            with contextlib.suppress(ApiException):
                core_v1.delete_namespaced_pod(
                    name=pod_name,
                    namespace=namespace,
                    grace_period_seconds=0,
                )

    FAILURE_CLASSES = {
        # Every one of these compares against a value the injector wrote or a
        # rotation it performed, so they attribute confidently: the credential
        # was rotated, and the agent's job was to make the app follow.
        "stale_pod_still_serving": FailureClass.AGENT_ERROR,
        "secret_not_rotated": FailureClass.AGENT_ERROR,
        "deployment_not_using_rotated_secret": FailureClass.AGENT_ERROR,
        "postgres_rejects_new_password": FailureClass.AGENT_ERROR,
        "postgres_still_accepts_old_password": FailureClass.AGENT_ERROR,
        "init_missing_rotated_password": FailureClass.AGENT_ERROR,
        # One HTTP request against a real app.
        "product_probe_failed": FailureClass.AMBIGUOUS,
        "postgres_password_probe_unreadable": FailureClass.AMBIGUOUS,
    }

    def evaluate(self, *args, **kwargs) -> dict:
        """Evaluate whether the required rotation reached a fresh, functional pod."""
        try:
            return self._evaluate()
        except Exception as exc:
            print(f"[FAIL] Error checking credential rotation: {exc}")
            return self.fail_from_exception(exc)

    def _evaluate(self) -> dict:
        print("== Secret Rotation Mitigation Evaluation ==")
        # ``success`` and ``reason`` are no longer pre-seeded: every exit path
        # goes through ``_reject`` or sets success explicitly, so a new early
        # return cannot silently inherit a stale failure verdict.
        results = {
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
        }

        output = self._run(f"kubectl get deployment {self.problem.faulty_service} -n {self.problem.namespace} -o json")
        deployment_json = json.loads(output)
        deployment = self.problem.kubectl.get_deployment(
            self.problem.faulty_service,
            self.problem.namespace,
        )
        results["deployment_exists"] = True

        desired = self._desired_replicas(deployment)
        if desired < 1:
            return self._reject(
                results, "required_deployment_scaled_to_zero", f"product-catalog is scaled to {desired}"
            )

        deployment = self._wait_for_current_rollout(deployment)
        if deployment is None:
            return self._reject(
                results, "required_deployment_not_rolled_out", "product-catalog did not complete its current rollout"
            )
        results["rollout_complete"] = True
        results["pods_ready"] = True

        endpoint_ready, current_pod_uids = self._ready_target_pod_uids(deployment)
        results["ready_target_endpoint"] = endpoint_ready
        results["current_pod_uids"] = sorted(current_pod_uids)
        if not endpoint_ready:
            return self._reject(
                results, "no_ready_endpoints", "product-catalog has no ready endpoint from its Deployment"
            )

        stale_pod_uid = self._stale_pod_uid(deployment_json)
        results["stale_pod_uid"] = stale_pod_uid
        if stale_pod_uid and stale_pod_uid in current_pod_uids:
            return self._reject(
                results,
                "stale_pod_still_serving",
                "the product-catalog pod from before credential rotation is still serving",
                pod_uid=stale_pod_uid,
            )

        secret_conn = self.problem._get_secret_conn_string()
        configured_conn = self._configured_connection_string(deployment_json, secret_conn)
        results["secret_conn"] = secret_conn
        results["configured_conn"] = configured_conn
        results["deployment_references_secret"] = self._deployment_references_secret(deployment_json)
        if secret_conn != self.new_conn:
            return self._reject(
                results, "secret_not_rotated", "the Secret does not contain the required rotated connection string"
            )
        if configured_conn != self.new_conn:
            return self._reject(
                results,
                "deployment_not_using_rotated_secret",
                "product-catalog is not configured with the required rotated connection string",
            )

        results["postgres_accepts_old_password"] = self._postgres_accepts_password(self.old_password)
        results["postgres_accepts_new_password"] = self._postgres_accepts_password(self.new_password)
        if results["postgres_accepts_old_password"] is None or results["postgres_accepts_new_password"] is None:
            return self._reject(
                results,
                "postgres_password_probe_unreadable",
                "PostgreSQL returned an unexpected password probe response",
            )
        results["postgresql_init_uses_new_password"] = self.problem._postgresql_init_uses_password(self.new_password)

        if not results["postgres_accepts_new_password"]:
            return self._reject(
                results, "postgres_rejects_new_password", "PostgreSQL does not accept the required rotated password"
            )
        if results["postgres_accepts_old_password"]:
            return self._reject(
                results, "postgres_still_accepts_old_password", "PostgreSQL still accepts the pre-rotation password"
            )
        if not results["postgresql_init_uses_new_password"]:
            return self._reject(
                results,
                "init_missing_rotated_password",
                "postgresql-init does not declare the required rotated password",
            )

        results["product_probe_succeeded"] = self._run_product_probe()
        if not results["product_probe_succeeded"]:
            return self._reject(
                results, "product_probe_failed", "a fresh /api/products request did not return catalog data"
            )

        results["success"] = True
        results["message"] = "required credential rotation is consistent and product queries succeed"
        print("Mitigation Result: Pass")
        return results

    def _reject(self, results: dict, reason: str, message: str, **detail) -> dict:
        """Merge a coded verdict into the diagnostics dict and keep the prose.

        Every branch here used to set a free-text ``results["reason"]`` -- some
        of them interpolating an exception string, so no two runs shared a
        value. The sentence stays, as ``message``; ``reason`` becomes a code.
        The rest of the diagnostics dict is untouched, since it is what makes
        this oracle's output worth reading.
        """
        print(f"❌ {message}")
        results.update(self.fail(reason, message=message, **detail))
        return results
