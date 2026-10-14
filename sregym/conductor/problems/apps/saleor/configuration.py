"""Saleor: configuration."""

from __future__ import annotations

import copy
import json
import shlex
import time

from sregym.conductor.oracles.apps.saleor.secret_rotation import SecretRotationStaleEnvMitigationIA
from sregym.conductor.oracles.dns_resolution_mitigation import DNSResolutionMitigationOracle
from sregym.conductor.oracles.env_variable_shadowing_mitigation import EnvVariableShadowingMitigationOracle
from sregym.conductor.oracles.mitigation import MitigationOracle
from sregym.conductor.oracles.wrong_bin_mitigation import WrongBinMitigationOracle
from sregym.conductor.problems.apps.saleor.helpers import API, API_SERVICE, APP, WORKER, _replace_deployment
from sregym.conductor.problems.env_variable_shadowing import EnvVariableShadowing
from sregym.conductor.problems.environment import configure_port
from sregym.conductor.problems.rbac_misconfiguration import RBACMisconfiguration
from sregym.conductor.problems.secret_rotation_stale_env_credentials import (
    SecretRotationStaleEnvCredentialsAstronomyShop,
)
from sregym.conductor.problems.sidecar_port_conflict import SidecarPortConflict
from sregym.conductor.problems.wrong_bin_usage import WrongBinUsage
from sregym.conductor.problems.wrong_dns_policy import WrongDNSPolicy
from sregym.generators.fault.inject_virtual import VirtualizationFaultInjector
from sregym.utils.decorators import mark_fault_injected


class WrongBinUsageSaleor(WrongBinUsage):
    """saleor-api runs the image's Celery worker entrypoint instead of uvicorn."""

    def __init__(self, faulty_service: str = API, wrong_binary_source: str = WORKER):
        self.faulty_service = faulty_service
        self.wrong_binary_source = wrong_binary_source
        # WrongBinMitigationOracle: the API container's command must run this binary again.
        self.expected_command = "uvicorn"
        # The injected command is the worker's Celery entrypoint; removing the
        # override (the image's default CMD runs uvicorn) is also a fix once the
        # Deployment rolls out Ready.
        self.wrong_command = "celery"
        configure_port(
            self,
            APP,
            component=f"deployment/{faulty_service}",
            description=(
                f"The container command of deployment `{faulty_service}` was replaced with the Celery worker "
                f"entrypoint of `{wrong_binary_source}` (`celery -A saleor --app=saleor.celeryconf:app worker ...`) "
                "instead of the ASGI server (`uvicorn saleor.asgi:application --port=8000 ...`). Both are valid "
                "binaries in the same image, so the pod starts, but nothing listens on port 8000: readiness "
                "(`/health/`) and liveness (tcp 8000) probes fail, the pod restarts and never becomes Ready, and "
                f"Service `{API_SERVICE}` has no endpoints, so all GraphQL API requests fail."
            ),
            oracle_factory=WrongBinMitigationOracle,
        )

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")
        injector = VirtualizationFaultInjector(namespace=self.namespace)
        source = injector._get_deployment_yaml(self.wrong_binary_source)
        wrong_command = source["spec"]["template"]["spec"]["containers"][0]["command"]
        deployment = injector._get_deployment_yaml(self.faulty_service)
        injector._write_yaml_to_file(self.faulty_service, copy.deepcopy(deployment))
        container = deployment["spec"]["template"]["spec"]["containers"][0]
        print(f"Changing {self.faulty_service}/{container['name']} command to {wrong_command}")
        container["command"] = list(wrong_command)
        _replace_deployment(injector, self.faulty_service, deployment)
        print(f"Service: {self.faulty_service} | Namespace: {self.namespace}\n")

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")
        self.kubectl.exec_command(f"kubectl delete deployment {self.faulty_service} -n {self.namespace}")
        self.kubectl.exec_command(f"kubectl apply -f /tmp/{self.faulty_service}_modified.yaml -n {self.namespace}")
        print(f"Service: {self.faulty_service} | Namespace: {self.namespace}\n")


class SidecarPortConflictSaleor(SidecarPortConflict):
    def __init__(self, faulty_service: str = API):
        self.faulty_service = faulty_service
        configure_port(
            self,
            APP,
            component=f"deployment/{faulty_service}",
            description=(
                f"Deployment `{faulty_service}` gained a sidecar container (`sidecar`, busybox `nc -lk -p 8000`) "
                "that binds the same port 8000 as the uvicorn API container in the same pod network namespace. "
                "Whichever process binds second fails (`address already in use`), so one container crash-loops and "
                f"the pod never becomes Ready; Service `{API_SERVICE}` has no endpoints and the GraphQL API is down."
            ),
            oracle_factory=MitigationOracle,
        )


class RBACMisconfigurationSaleor(RBACMisconfiguration):
    """The init container's kubectl needs a mounted token to reach the RBAC check, so automount is forced on."""

    def __init__(self, faulty_service: str = API):
        self.faulty_service = faulty_service
        configure_port(
            self,
            APP,
            component=f"deployment/{faulty_service}",
            description=(
                f"Deployment `{faulty_service}` now runs as ServiceAccount `{faulty_service}-rbac-sa` and has an "
                "init container `config-loader` that runs `kubectl get configmap app-routing-config`. The "
                f"ClusterRole `{faulty_service}-rbac-role` bound to that ServiceAccount only grants get/list/watch "
                "on pods and services, not configmaps, so the init container fails with a Forbidden error and the "
                f"pod stays in Init:Error/Init:CrashLoopBackOff; Service `{API_SERVICE}` has no endpoints and the "
                "GraphQL API is down. The fix is to grant configmap read access to the ServiceAccount (or remove the "
                "dependency), not to delete the deployment."
            ),
            oracle_factory=MitigationOracle,
        )

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection: RBAC Init Container Misconfiguration ==")
        injector = VirtualizationFaultInjector(namespace=self.namespace)
        original = injector._get_deployment_yaml(self.faulty_service)
        # Incident Arena charts may disable automountServiceAccountToken; the new ServiceAccount's token
        # must be mounted for the init container to reach the API server and be denied by RBAC.
        self.kubectl.patch_deployment(
            self.faulty_service,
            self.namespace,
            {"spec": {"template": {"spec": {"automountServiceAccountToken": True}}}},
        )
        injector._inject(fault_type="rbac_misconfiguration", microservices=[self.faulty_service])
        # Recovery re-applies the true original (token automount off), not the patched copy.
        injector._write_yaml_to_file(f"{self.faulty_service}-original", original)
        print(f"Service: {self.faulty_service} | Namespace: {self.namespace}\n")


class EnvVariableShadowingIA(EnvVariableShadowing):
    """A second ``DATABASE_URL`` definition points Saleor's API at localhost."""

    def __init__(
        self,
        app_name: str = "saleor",
        faulty_service: str = "saleor-api",
        container_name: str = "api",
        service_name: str = "svc-saleor-api",
        env_name: str = "DATABASE_URL",
        expected_value: str = "postgres://saleor_app:agentrepair-app@postgres:5432/saleor",
        shadow_value: str = "postgres://saleor_app:agentrepair-app@localhost:5432/saleor",
        health_path: str = "/health/",
        expected_content: str = "",
    ):
        self.faulty_service = faulty_service
        self.container_name = container_name
        self.service_name = service_name
        self.ENV_NAME = env_name
        self.EXPECTED_VALUE = expected_value
        self.SHADOW_VALUE = shadow_value
        self.health_path = health_path
        self.expected_content = expected_content
        self._baseline_template = None
        configure_port(
            self,
            app_name,
            component=f"deployment/{faulty_service}",
            description=(
                f"Container `{container_name}` of deployment `{faulty_service}` defines `{env_name}` twice. The "
                f"later definition (`{shadow_value}`) shadows the intended one (`{expected_value}`), so the API "
                "connects to a database on localhost that does not exist instead of the `postgres` Service, and "
                "its requests fail although the earlier, correct definition still appears in the pod spec."
            ),
            oracle_factory=EnvVariableShadowingMitigationOracle,
        )

    def _target_container_and_env(self, deployment):
        for container_index, container in enumerate(deployment.spec.template.spec.containers):
            if container.name == self.container_name:
                return container_index, container.env or []
        raise RuntimeError(f"Container '{self.container_name}' was not found")

    def _wait_for_rollout(self):
        self.kubectl.exec_command(
            f"kubectl rollout status deployment/{self.faulty_service} -n {self.namespace} --timeout=300s"
        )


class WrongDNSPolicyIA(WrongDNSPolicy):
    """Saleor's API pods get an external-only resolver.

    Saleor's readiness endpoint does not touch its backends, so the
    misconfigured pod becomes Ready and serves, as Astronomy Shop's frontend
    did. (Frappe and most Slack roles probe their backends by name, so a
    resolver change there only stalls the rollout behind the healthy pod.)
    """

    def __init__(
        self, app_name: str = "saleor", faulty_service: str = "saleor-api", service_name: str = "svc-saleor-api"
    ):
        self.faulty_service = faulty_service
        self.service_name = service_name
        configure_port(
            self,
            app_name,
            component=faulty_service,
            description=(
                f"Deployment `{faulty_service}` is configured with `dnsPolicy: None` and an external resolver "
                "(`8.8.8.8`), so its pods cannot resolve cluster-internal service names such as `postgres`, "
                "`valkey` and `rabbitmq`. Its health endpoint does not touch those dependencies, so the pod is "
                "Ready and receives traffic, but every request that needs the database fails with name-resolution "
                "errors even though the dependencies are healthy."
            ),
            oracle_factory=DNSResolutionMitigationOracle,
        )


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
        configure_port(
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
