"""Secret rotation stale environment credential problem for Astronomy Shop."""

import base64
import json
import logging
import shlex
import time

from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.oracles.secret_rotation_stale_env_mitigation import SecretRotationStaleEnvMitigation
from sregym.conductor.problems.base import Problem
from sregym.service.apps.astronomy_shop import AstronomyShop
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected

logger = logging.getLogger(__name__)


class SecretRotationStaleEnvCredentialsAstronomyShop(Problem):
    """Rotate product-catalog database credentials without refreshing the running pod environment."""

    SOURCE_POD_UID_ANNOTATION = "credential-source-pod-uid"
    _POSTGRES_ROTATION_ATTEMPTS = 3
    _POSTGRES_PASSWORD_CHECK_ATTEMPTS = 3
    _POSTGRES_PASSWORD_CHECK_INTERVAL_SECONDS = 3
    _VERIFY_TIMEOUT_SECONDS = 30

    def __init__(self):
        """Configure the fixed Astronomy Shop secret-rotation problem."""
        self.app = AstronomyShop()
        self.namespace = self.app.namespace
        super().__init__(app=self.app, namespace=self.namespace)
        self.kubectl = KubeCtl()

        self.faulty_service = "product-catalog"
        self.backend_service = "postgresql"
        self.secret_name = "product-catalog-db-conn"
        self.secret_key = "DB_CONNECTION_STRING"
        self.postgresql_init_configmap = "postgresql-init"
        self.postgresql_init_key = "init.sql"
        self.db_user = "otelu"
        self.db_name = "otel"
        self.old_password = "otelp"
        self.new_password = "otelp_7k9m2q4x"
        self.old_conn = "postgres://otelu:otelp@postgresql/otel?sslmode=disable"
        self.new_conn = "postgres://otelu:otelp_7k9m2q4x@postgresql/otel?sslmode=disable"
        self.literal_db_clients = {
            "accounting": (
                "Host=postgresql;Username=otelu;Password=otelp;Database=otel",
                "Host=postgresql;Username=otelu;Password=otelp_7k9m2q4x;Database=otel",
            ),
            "product-reviews": (
                "host=postgresql user=otelu password=otelp dbname=otel",
                "host=postgresql user=otelu password=otelp_7k9m2q4x dbname=otel",
            ),
        }
        self.stale_product_catalog_pod_uid: str | None = None
        self.root_cause = self.build_structured_root_cause(
            component=f"deployment/{self.faulty_service}",
            namespace=self.namespace,
            description=(
                "PostgreSQL credentials and the Kubernetes Secret were rotated, but the active product-catalog pod "
                "continues using the database connection string captured at container startup. After existing "
                "database sessions are terminated, product-catalog cannot reconnect until its runtime credential is "
                "made consistent with the backend."
            ),
        )

        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)
        self.app.create_workload()
        self.mitigation_oracle = SecretRotationStaleEnvMitigation(problem=self)

    def _run(self, command: str, input_data: str | None = None) -> str:
        """Run a kubectl command through the repository wrapper."""
        logger.debug("[secret-rotation] %s", command)
        return self.kubectl.exec_command(command, input_data=input_data)

    def _apply_secret(self, conn_string: str) -> None:
        """Create or update the DB connection Secret with the given connection string."""
        literal = shlex.quote(f"{self.secret_key}={conn_string}")

        manifest = self._run(
            f"kubectl create secret generic {self.secret_name} -n {self.namespace} "
            f"--from-literal={literal} --dry-run=client -o yaml"
        )

        self._run("kubectl apply -f -", input_data=manifest)

    def _set_product_catalog_secret_env(self) -> None:
        """Make product-catalog read DB_CONNECTION_STRING from the Secret."""
        output = self._run(
            f"kubectl set env deployment/{self.faulty_service} -n {self.namespace} "
            f"--containers={self.faulty_service} --from=secret/{self.secret_name} --keys={self.secret_key}"
        )
        if "error" in output.lower() or "invalid" in output.lower():
            raise RuntimeError(f"Failed to set {self.secret_key} from Secret: {output}")

    def _set_product_catalog_literal_env(self, conn_string: str) -> None:
        """Make product-catalog use a literal DB_CONNECTION_STRING value (used for reset)."""
        output = self._run(
            f"kubectl set env deployment/{self.faulty_service} -n {self.namespace} "
            f"--containers={self.faulty_service} {self.secret_key}={shlex.quote(conn_string)}"
        )
        if "error" in output.lower() or "invalid" in output.lower():
            raise RuntimeError(f"Failed to set literal {self.secret_key}: {output}")

    def _set_literal_db_clients_password(self, password: str) -> None:
        """Update non-faulty pods' credentials so only product-catalog keeps wrong credentials."""
        conn_index = 0 if password == self.old_password else 1
        for deployment, conn_strings in self.literal_db_clients.items():
            output = self._run(
                f"kubectl set env deployment/{deployment} -n {self.namespace} "
                f"--containers={deployment} {self.secret_key}={shlex.quote(conn_strings[conn_index])}"
            )
            if "error" in output.lower() or "invalid" in output.lower():
                raise RuntimeError(f"Failed to set literal {self.secret_key} for {deployment}: {output}")
            self._run(f"kubectl rollout status deployment/{deployment} -n {self.namespace} --timeout=180s")

    def _rollout_restart(self, deployment: str, timeout: str = "180s") -> None:
        """Restart a Deployment and wait for its rollout to finish."""
        self._run(f"kubectl rollout restart deployment/{deployment} -n {self.namespace}")
        self._run(f"kubectl rollout status deployment/{deployment} -n {self.namespace} --timeout={timeout}")

    def _postgres_exec(self, password: str, sql_or_query: str, tuples_only: bool = False) -> str:
        """Run a psql command against PostgreSQL using the specified password."""
        psql_args = "-tAc" if tuples_only else "-c"
        script = (
            f"PGPASSWORD={shlex.quote(password)} psql -h {shlex.quote(self.backend_service)} "
            f"-U {shlex.quote(self.db_user)} -d {shlex.quote(self.db_name)} "
            f"{psql_args} {shlex.quote(sql_or_query)}"
        )
        return self._run(
            f"kubectl exec -n {self.namespace} deploy/{self.backend_service} -- sh -lc {shlex.quote(script)}"
        )

    def _rotate_postgres_password(self, from_password: str, to_password: str) -> None:
        """Rotate the PostgreSQL password for the application user."""
        last_output = ""
        for attempt in range(self._POSTGRES_ROTATION_ATTEMPTS):
            last_output = self._postgres_exec(
                from_password,
                f"ALTER USER {self.db_user} WITH PASSWORD '{to_password}';",
            )
            if "ALTER ROLE" in last_output or self._postgres_accepts_password(to_password):
                return
            if attempt < self._POSTGRES_ROTATION_ATTEMPTS - 1:
                time.sleep(self._POSTGRES_PASSWORD_CHECK_INTERVAL_SECONDS)
        raise RuntimeError(f"Failed to rotate PostgreSQL password for {self.db_user}: {last_output}")

    def _drop_postgres_connections(self, password: str) -> None:
        """Terminate existing app DB sessions so product-catalog reconnect."""
        output = self._postgres_exec(
            password,
            f"select pg_terminate_backend(pid) from pg_stat_activity "
            f"where usename = '{self.db_user}' and pid <> pg_backend_pid();",
            tuples_only=True,
        )
        if "FATAL" in output or "ERROR" in output:
            raise RuntimeError(f"Failed to terminate existing PostgreSQL sessions: {output}")

    def _postgres_accepts_password(self, password: str) -> bool:
        """Return whether PostgreSQL accepts the given password for otelu."""
        script = (
            f"if PGPASSWORD={shlex.quote(password)} psql -h {shlex.quote(self.backend_service)} "
            f"-U {shlex.quote(self.db_user)} -d {shlex.quote(self.db_name)} -tAc 'select 1' >/dev/null 2>&1; "
            "then echo 1; else echo 0; fi"
        )
        command = f"kubectl exec -n {self.namespace} deploy/{self.backend_service} -- sh -lc {shlex.quote(script)}"
        for attempt in range(self._POSTGRES_PASSWORD_CHECK_ATTEMPTS):
            output = self._run(command)
            if output.strip() == "1":
                return True
            if attempt < self._POSTGRES_PASSWORD_CHECK_ATTEMPTS - 1:
                time.sleep(self._POSTGRES_PASSWORD_CHECK_INTERVAL_SECONDS)
        return False

    def _get_postgresql_init_sql(self) -> str | None:
        """Return the live PostgreSQL init from the postgresql-init ConfigMap."""
        output = self._run(
            f"kubectl get configmap {self.postgresql_init_configmap} -n {self.namespace} "
            "-o jsonpath='{.data.init\\.sql}'"
        )
        if "not found" in output.lower() or "error from server" in output.lower():
            return None
        return output or None

    def _postgresql_init_uses_password(self, password: str) -> bool:
        """Return whether postgresql-init would recreate the app user with the new password."""
        init_sql = self._get_postgresql_init_sql() or ""
        expected_line = f"CREATE USER {self.db_user} WITH PASSWORD '{password}';"
        other_passwords = (item for item in (self.old_password, self.new_password) if item != password)
        return expected_line in init_sql and not any(
            f"CREATE USER {self.db_user} WITH PASSWORD '{other_password}';" in init_sql
            for other_password in other_passwords
        )

    def _patch_postgresql_init_password(self, password: str) -> None:
        """Update live postgresql-init bootstrap SQL to recreate otelu with the new password."""
        init_sql = self._get_postgresql_init_sql()
        if not init_sql:
            raise RuntimeError(
                f"ConfigMap {self.postgresql_init_configmap}/{self.postgresql_init_key} is missing or empty."
            )

        from_line = f"CREATE USER {self.db_user} WITH PASSWORD '{self.old_password}';"
        to_line = f"CREATE USER {self.db_user} WITH PASSWORD '{self.new_password}';"
        if password == self.old_password:
            from_line, to_line = to_line, from_line
        if from_line not in init_sql:
            if to_line in init_sql:
                return
            raise RuntimeError(f"Could not find {self.db_user} password declaration in {self.postgresql_init_key}.")

        updated_sql = init_sql.replace(from_line, to_line)
        patch = json.dumps({"data": {self.postgresql_init_key: updated_sql}})
        output = self._run(
            f"kubectl patch configmap {self.postgresql_init_configmap} -n {self.namespace} "
            f"--type=merge -p {shlex.quote(patch)}"
        )
        if "error" in output.lower() or "invalid" in output.lower():
            raise RuntimeError(f"Failed to patch {self.postgresql_init_configmap}: {output}")

    def _get_product_catalog_pod(self):
        """Return the current product-catalog pod object, preferring a running pod."""
        fallback = None
        for pod in self.kubectl.list_pods(self.namespace).items:
            if "product-catalog" not in (pod.metadata.name or ""):
                continue
            fallback = fallback or pod
            if pod.metadata.deletion_timestamp:
                continue
            if pod.status.phase == "Running":
                return pod
        return fallback

    def _get_stale_product_catalog_pod_uid(self) -> str | None:
        """Read the credential source pod identity from Deployment metadata."""
        deployment = self.kubectl.get_deployment(self.faulty_service, self.namespace)
        annotations = deployment.metadata.annotations or {}
        return annotations.get(self.SOURCE_POD_UID_ANNOTATION)

    def _set_stale_product_catalog_pod_uid(self, pod_uid: str) -> None:
        """Persist the credential source pod identity without changing the pod template."""
        self.kubectl.apps_v1_api.patch_namespaced_deployment(
            name=self.faulty_service,
            namespace=self.namespace,
            body={"metadata": {"annotations": {self.SOURCE_POD_UID_ANNOTATION: pod_uid}}},
        )
        self.stale_product_catalog_pod_uid = pod_uid

    def _clear_stale_product_catalog_pod_uid(self) -> None:
        """Remove the credential source marker after the pod is refreshed."""
        self.kubectl.apps_v1_api.patch_namespaced_deployment(
            name=self.faulty_service,
            namespace=self.namespace,
            body={"metadata": {"annotations": {self.SOURCE_POD_UID_ANNOTATION: None}}},
        )
        self.stale_product_catalog_pod_uid = None

    def _get_product_catalog_env(self) -> str | None:
        """Infer product-catalog's effective DB env value from Deployment, Secret, and pod UID."""
        pod = self._get_product_catalog_pod()
        if not pod:
            return None

        deployment = json.loads(self._run(f"kubectl get deployment {self.faulty_service} -n {self.namespace} -o json"))
        containers = deployment["spec"]["template"]["spec"]["containers"]
        container = next(
            (item for item in containers if item.get("name") == self.faulty_service),
            containers[0],
        )
        for item in container.get("env", []):
            if item.get("name") != self.secret_key:
                continue
            if "value" in item:
                return item["value"]
            secret_ref = item.get("valueFrom", {}).get("secretKeyRef", {})
            if secret_ref.get("name") != self.secret_name or secret_ref.get("key") != self.secret_key:
                return None
            secret_conn = self._get_secret_conn_string()
            stale_pod_uid = self.stale_product_catalog_pod_uid or self._get_stale_product_catalog_pod_uid()
            if secret_conn == self.new_conn and pod.metadata.uid == stale_pod_uid:
                return self.old_conn
            return secret_conn
        return None

    def _product_catalog_pods_ready(self) -> bool:
        """Return whether all current product-catalog pods are Running and Ready."""
        found = False
        for pod in self.kubectl.list_pods(self.namespace).items:
            if "product-catalog" not in (pod.metadata.name or "") or pod.metadata.deletion_timestamp:
                continue
            found = True
            statuses = pod.status.container_statuses or []
            if pod.status.phase != "Running":
                return False
            if not statuses or not all(status.ready for status in statuses):
                return False
        return found

    def _get_secret_conn_string(self) -> str | None:
        """Decode DB_CONNECTION_STRING from the Kubernetes Secret."""
        output = self._run(f"kubectl get secret {self.secret_name} -n {self.namespace} -o json")
        if "not found" in output.lower() or "error from server" in output.lower():
            return None
        secret = json.loads(output)
        encoded = secret.get("data", {}).get(self.secret_key)
        if not encoded:
            return None
        return base64.b64decode(encoded).decode("utf-8").strip()

    def _recover_to_baseline(self) -> None:
        """Restore old DB credentials and restart product-catalog from a clean baseline."""
        logger.info("[secret-rotation] Recovering previous secret-rotation state.")
        if not self._postgres_accepts_password(self.old_password):
            try:
                self._rotate_postgres_password(self.new_password, self.old_password)
            except RuntimeError as exc:
                raise RuntimeError(f"Could not restore PostgreSQL password to the baseline value: {exc}") from exc
            if not self._postgres_accepts_password(self.old_password):
                raise RuntimeError("PostgreSQL password restore did not make the baseline password valid.")

        self._patch_postgresql_init_password(self.old_password)
        self._set_literal_db_clients_password(self.old_password)
        self._set_product_catalog_literal_env(self.old_conn)
        self._run(f"kubectl delete secret {self.secret_name} -n {self.namespace} --ignore-not-found")
        self._rollout_restart(self.faulty_service)
        self._clear_stale_product_catalog_pod_uid()

    @mark_fault_injected
    def inject_fault(self):
        """Inject stale product-catalog DB credentials after rotating PostgreSQL and the Secret."""
        print("== Fault Injection ==")
        self._recover_to_baseline()

        logger.info("[secret-rotation] Creating baseline Secret with old DB credentials.")
        self._apply_secret(self.old_conn)

        logger.info("[secret-rotation] Patching product-catalog to read DB_CONNECTION_STRING from the Secret.")
        self._set_product_catalog_secret_env()
        self._rollout_restart(self.faulty_service)
        pod = self._get_product_catalog_pod()
        if pod is None or not pod.metadata.uid:
            raise RuntimeError("Could not identify the baseline product-catalog pod")
        self._set_stale_product_catalog_pod_uid(pod.metadata.uid)

        product_env = self._get_product_catalog_env()
        if product_env != self.old_conn:
            raise RuntimeError(f"product-catalog did not start with old connection string; got {product_env!r}")

        logger.info("[secret-rotation] Rotating PostgreSQL backend password to the new password.")
        self._rotate_postgres_password(self.old_password, self.new_password)

        logger.info("[secret-rotation] Updating Kubernetes Secret to the new DB connection string.")
        self._apply_secret(self.new_conn)

        logger.info("[secret-rotation] Updating PostgreSQL bootstrap ConfigMap to the new DB password.")
        self._patch_postgresql_init_password(self.new_password)

        logger.info("[secret-rotation] Updating non-faulty literal DB clients to the new DB password.")
        self._set_literal_db_clients_password(self.new_password)

        logger.info("[secret-rotation] Terminating existing PostgreSQL sessions to force product-catalog reconnect.")
        self._drop_postgres_connections(self.new_password)

        deadline = time.monotonic() + self._VERIFY_TIMEOUT_SECONDS
        while True:
            state = {
                "secret_is_new": self._get_secret_conn_string() == self.new_conn,
                "product_env_is_old": self._get_product_catalog_env() == self.old_conn,
                "product_pods_ready": self._product_catalog_pods_ready(),
                "postgres_new_password": self._postgres_accepts_password(self.new_password),
                "postgres_old_password_rejected": not self._postgres_accepts_password(self.old_password),
                "postgresql_init_is_new": self._postgresql_init_uses_password(self.new_password),
            }
            if all(state.values()):
                break
            if time.monotonic() >= deadline:
                break
            time.sleep(self._POSTGRES_PASSWORD_CHECK_INTERVAL_SECONDS)
        logger.info("[secret-rotation] Verification: %s", state)
        if not all(state.values()):
            raise RuntimeError(f"Fault verification failed; stale env state was not created: {state}")

        print(f"Service: {self.faulty_service} | Namespace: {self.namespace}\n")

    @mark_fault_injected
    def recover_fault(self):
        """Recover the service configuration and restart product-catalog."""
        print("== Fault Recovery ==")
        if not self._postgres_accepts_password(self.new_password):
            self._rotate_postgres_password(self.old_password, self.new_password)

        self._apply_secret(self.new_conn)
        self._patch_postgresql_init_password(self.new_password)
        self._set_literal_db_clients_password(self.new_password)
        self._set_product_catalog_secret_env()
        self._rollout_restart(self.faulty_service)
        self._clear_stale_product_catalog_pod_uid()

        print(f"Service: {self.faulty_service} | Namespace: {self.namespace}\n")
