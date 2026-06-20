import json
import logging
import shlex
import time
from urllib.parse import urlparse

from sregym.conductor.oracles.base import Oracle

logger = logging.getLogger(__name__)


class SecretRotationStaleEnvMitigation(Oracle):
    """Evaluate whether product-catalog and PostgreSQL credentials are consistent."""

    importance = 1.0

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

    def _deployment_references_secret(self, deployment: dict) -> bool:
        """Return whether product-catalog still sources DB_CONNECTION_STRING from the Secret."""
        containers = deployment.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
        for container in containers:
            for env in container.get("env", []):
                if env.get("name") != self.problem.secret_key:
                    continue
                secret_ref = env.get("valueFrom", {}).get("secretKeyRef", {})
                return (
                    secret_ref.get("name") == self.problem.secret_name
                    and secret_ref.get("key") == self.problem.secret_key
                )
        return False

    def _password_from_conn_string(self, conn_string: str | None) -> str | None:
        """Extract the password portion from a PostgreSQL connection string."""
        if not conn_string:
            return None
        if conn_string.startswith(("postgres://", "postgresql://")):
            return urlparse(conn_string).password
        return None

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

    def evaluate(self, *args, **kwargs) -> dict:
        """Evaluate whether credentials are consistent and product-catalog is healthy."""
        print("== Mitigation Evaluation ==")
        results = {
            "success": False,
            "deployment_exists": False,
            "pods_ready": False,
            "secret_conn": None,
            "product_env_conn": None,
            "deployment_references_secret": False,
            "postgres_accepts_product_env_password": False,
            "postgres_accepts_old_password": False,
            "postgres_accepts_new_password": False,
            "postgresql_init_uses_new_password": False,
            "reason": "",
        }

        output = self._run(f"kubectl get deployment {self.problem.faulty_service} -n {self.problem.namespace} -o json")
        try:
            deployment = json.loads(output)
        except json.JSONDecodeError:
            results["reason"] = "product-catalog deployment does not exist"
            return results
        results["deployment_exists"] = True
        results["deployment_references_secret"] = self._deployment_references_secret(deployment)

        results["pods_ready"] = self.problem._product_catalog_pods_ready()
        if not results["pods_ready"]:
            results["reason"] = "product-catalog pods are not Running/Ready"
            return results

        try:
            env_conn = self.problem._get_product_catalog_env()
        except RuntimeError as exc:
            results["reason"] = str(exc)
            return results
        secret_conn = self.problem._get_secret_conn_string()
        results["secret_conn"] = secret_conn
        results["product_env_conn"] = env_conn

        results["postgres_accepts_old_password"] = self._postgres_accepts_password(self.old_password)
        results["postgres_accepts_new_password"] = self._postgres_accepts_password(self.new_password)
        results["postgres_accepts_product_env_password"] = self._postgres_accepts_password(
            self._password_from_conn_string(env_conn)
        )
        results["postgresql_init_uses_new_password"] = self.problem._postgresql_init_uses_password(self.new_password)

        if not results["postgres_accepts_new_password"]:
            results["reason"] = "PostgreSQL does not accept the expected rotated password"
            return results
        if not results["postgresql_init_uses_new_password"]:
            results["reason"] = "postgresql-init no longer declares the expected rotated password"
            return results
        if secret_conn == self.new_conn and env_conn == self.old_conn and results["postgres_accepts_new_password"]:
            results["reason"] = "product-catalog still has stale old env while Secret/PostgreSQL use new credential"
            return results
        if env_conn not in {self.old_conn, self.new_conn}:
            results["reason"] = f"unexpected product-catalog DB_CONNECTION_STRING: {env_conn!r}"
            return results
        if not results["postgres_accepts_product_env_password"]:
            results["reason"] = "PostgreSQL does not accept the password product-catalog is using"
            return results
        if results["deployment_references_secret"] and secret_conn not in {None, env_conn}:
            results["reason"] = "product-catalog references Secret, but Secret and running env disagree"
            return results

        results["success"] = True
        results["reason"] = "credentials are consistent and product-catalog is healthy"
        print("Mitigation Result: Pass")
        return results
