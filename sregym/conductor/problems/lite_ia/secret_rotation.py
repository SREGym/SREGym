"""SREGym-Lite's stale-credential secret rotation, re-targeted at Saleor.

The original rotates Astronomy Shop's ``otelu`` Postgres password everywhere
except the running product-catalog pod. Here the rotated credential is
Saleor's ``saleor_app`` role: the Postgres role password, the API's Secret,
the ``postgres-init-scripts`` bootstrap SQL and the Celery worker's literal
``DATABASE_URL`` all move to the new password, while the running
``saleor-api`` pod keeps the old one in its environment. Saleor opens a new
database connection per request, so every API request then fails
authentication until the pod is restarted onto the rotated Secret.
"""

from __future__ import annotations

import json
import shlex
import time

from sregym.conductor.oracles.secret_rotation_stale_env_mitigation import SecretRotationStaleEnvMitigation
from sregym.conductor.problems.lite_ia.k8s import ported
from sregym.conductor.problems.secret_rotation_stale_env_credentials import (
    SecretRotationStaleEnvCredentialsAstronomyShop,
)


class SecretRotationStaleEnvMitigationIA(SecretRotationStaleEnvMitigation):
    """The original checks; user-facing health comes from ``LoadgenHealthOracle``."""

    run_product_probe = False


class SecretRotationStaleEnvCredentialsIA(SecretRotationStaleEnvCredentialsAstronomyShop):
    """Rotate ``saleor_app``'s password without refreshing the running ``saleor-api`` pod."""

    def __init__(
        self,
        app_name: str = "saleor",
        faulty_service: str = "saleor-api",
        container_name: str = "api",
        service_name: str = "svc-saleor-api",
    ):
        self.faulty_service = faulty_service
        self.container_name = container_name
        self.service_name = service_name
        self.backend_service = "postgres"
        self.postgres_exec_target = "statefulset/postgres"
        self.postgres_container = "postgres"
        self.secret_name = "saleor-api-db-conn"
        self.secret_key = "DATABASE_URL"
        self.postgresql_init_configmap = "postgres-init-scripts"
        self.postgresql_init_key = "10-app-role.sql"
        self.db_user = "saleor_app"
        self.db_name = "saleor"
        self.old_password = "agentrepair-app"
        self.new_password = "agentrepair-app-r7k2m9q4"
        self.old_conn = f"postgres://{self.db_user}:{self.old_password}@postgres:5432/{self.db_name}"
        self.new_conn = f"postgres://{self.db_user}:{self.new_password}@postgres:5432/{self.db_name}"
        # Sibling clients that hold the credential as a literal env value:
        # deployment -> (container, old value, new value).
        self.literal_db_clients = {
            "saleor-worker": ("worker", self.old_conn, self.new_conn),
        }
        self.stale_product_catalog_pod_uid = None
        ported(
            self,
            app_name,
            component=f"deployment/{faulty_service}",
            description=(
                f"The password of the Postgres role `{self.db_user}` was rotated, and the rotation was applied to "
                f"the Postgres role itself, the Secret `{self.secret_name}` (key `{self.secret_key}`), the "
                f"bootstrap ConfigMap `{self.postgresql_init_configmap}` and the Celery worker's `DATABASE_URL`. "
                f"The running `{faulty_service}` pod was not restarted: its container `{container_name}` still "
                f"holds the pre-rotation connection string in its `{self.secret_key}` environment variable, which "
                "was resolved from the Secret when the pod started. Saleor opens a new database connection per "
                "request, so every API request that touches the database fails Postgres password authentication "
                "and the storefront/GraphQL API returns errors, while the pod itself stays Running and Ready. The "
                "Deployment must be restarted so a fresh pod reads the rotated Secret."
            ),
            oracle_factory=SecretRotationStaleEnvMitigationIA,
        )

    # --- Saleor names for the original's kubectl helpers -------------------

    def _check(self, output: str, what: str) -> None:
        lowered = output.lower()
        if "error" in lowered or "invalid" in lowered:
            raise RuntimeError(f"{what}: {output}")

    def _set_product_catalog_secret_env(self) -> None:
        self._check(
            self._run(
                f"kubectl set env deployment/{self.faulty_service} -n {self.namespace} "
                f"--containers={self.container_name} --from=secret/{self.secret_name} --keys={self.secret_key}"
            ),
            f"Failed to set {self.secret_key} from Secret",
        )

    def _set_product_catalog_literal_env(self, conn_string: str) -> None:
        self._check(
            self._run(
                f"kubectl set env deployment/{self.faulty_service} -n {self.namespace} "
                f"--containers={self.container_name} {self.secret_key}={shlex.quote(conn_string)}"
            ),
            f"Failed to set literal {self.secret_key}",
        )

    def _set_literal_db_clients_password(self, password: str) -> None:
        use_old = password == self.old_password
        for deployment, (container, old_value, new_value) in self.literal_db_clients.items():
            value = old_value if use_old else new_value
            self._check(
                self._run(
                    f"kubectl set env deployment/{deployment} -n {self.namespace} "
                    f"--containers={container} {self.secret_key}={shlex.quote(value)}"
                ),
                f"Failed to set literal {self.secret_key} for {deployment}",
            )
            self._run(f"kubectl rollout status deployment/{deployment} -n {self.namespace} --timeout=300s")

    def _rollout_restart(self, deployment: str, timeout: str = "300s") -> None:
        super()._rollout_restart(deployment, timeout)

    def _psql_command(self, password: str, args: str) -> str:
        script = (
            f"PGPASSWORD={shlex.quote(password)} PGCONNECT_TIMEOUT=5 psql -X -w "
            f"-h {shlex.quote(self.backend_service)} -U {shlex.quote(self.db_user)} -d {shlex.quote(self.db_name)} "
            f"{args}"
        )
        return (
            f"kubectl exec -n {self.namespace} {self.postgres_exec_target} -c {self.postgres_container} -- "
            f"sh -c {shlex.quote(script)}"
        )

    def _postgres_exec(self, password: str, sql_or_query: str, tuples_only: bool = False) -> str:
        flag = "-tAc" if tuples_only else "-c"
        return self._run(self._psql_command(password, f"{flag} {shlex.quote(sql_or_query)}"))

    def _postgres_accepts_password(self, password: str) -> bool:
        command = self._psql_command(password, "-tAc 'select 1' >/dev/null 2>&1 && echo 1 || echo 0")
        for attempt in range(self._POSTGRES_PASSWORD_CHECK_ATTEMPTS):
            if self._run(command).strip() == "1":
                return True
            if attempt < self._POSTGRES_PASSWORD_CHECK_ATTEMPTS - 1:
                time.sleep(self._POSTGRES_PASSWORD_CHECK_INTERVAL_SECONDS)
        return False

    # --- Bootstrap SQL: ``CREATE ROLE saleor_app LOGIN PASSWORD '...'`` -----

    def _init_line(self, password: str) -> str:
        return f"CREATE ROLE {self.db_user} LOGIN PASSWORD '{password}'"

    def _get_postgresql_init_sql(self) -> str | None:
        output = self._run(f"kubectl get configmap {self.postgresql_init_configmap} -n {self.namespace} -o json")
        if "not found" in output.lower() or "error from server" in output.lower():
            return None
        try:
            return (json.loads(output).get("data") or {}).get(self.postgresql_init_key) or None
        except json.JSONDecodeError:
            return None

    def _postgresql_init_uses_password(self, password: str) -> bool:
        init_sql = self._get_postgresql_init_sql() or ""
        others = [item for item in (self.old_password, self.new_password) if item != password]
        return self._init_line(password) in init_sql and not any(self._init_line(other) in init_sql for other in others)

    def _patch_postgresql_init_password(self, password: str) -> None:
        init_sql = self._get_postgresql_init_sql()
        if not init_sql:
            raise RuntimeError(
                f"ConfigMap {self.postgresql_init_configmap}/{self.postgresql_init_key} is missing or empty."
            )
        other = self.new_password if password == self.old_password else self.old_password
        from_line, to_line = self._init_line(other), self._init_line(password)
        if from_line not in init_sql:
            if to_line in init_sql:
                return
            raise RuntimeError(f"Could not find {self.db_user} password declaration in {self.postgresql_init_key}.")
        patch = json.dumps({"data": {self.postgresql_init_key: init_sql.replace(from_line, to_line)}})
        self._check(
            self._run(
                f"kubectl patch configmap {self.postgresql_init_configmap} -n {self.namespace} "
                f"--type=merge -p {shlex.quote(patch)}"
            ),
            f"Failed to patch {self.postgresql_init_configmap}",
        )

    # --- Target pods by the Deployment's selector, not by name -------------

    def _target_pods(self):
        deployment = self.kubectl.get_deployment(self.faulty_service, self.namespace)
        selector = deployment.spec.selector.match_labels or {}
        return [
            pod
            for pod in self.kubectl.list_pods(self.namespace).items
            if all((pod.metadata.labels or {}).get(k) == v for k, v in selector.items())
        ]

    def _get_product_catalog_pod(self):
        fallback = None
        for pod in self._target_pods():
            fallback = fallback or pod
            if pod.metadata.deletion_timestamp:
                continue
            if pod.status.phase == "Running":
                return pod
        return fallback

    def _product_catalog_pods_ready(self) -> bool:
        found = False
        for pod in self._target_pods():
            if pod.metadata.deletion_timestamp:
                continue
            found = True
            statuses = pod.status.container_statuses or []
            if pod.status.phase != "Running" or not statuses or not all(s.ready for s in statuses):
                return False
        return found
