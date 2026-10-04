"""SREGym problems ported to Frappe/ERPNext (Incident Arena), environment scaling.

Every port keeps the original fault's causal mechanism and its state-based
mitigation check, re-targeted at a real Frappe component, and is wrapped with
the load generator health oracle by ``ported()``. The load generator drives the
Frappe Desk API at ``svc-frappe-web:8000`` (gunicorn, Deployment ``erp-gunicorn``),
which reaches MariaDB (``frappe-mariadb-subchart``) and both Redis brokers.
"""

from __future__ import annotations

import base64
import datetime
import json
import shlex
import textwrap
import time
from pathlib import Path

from kubernetes import client

from sregym.conductor.oracles.conntrack_mitigation import ConntrackMitigationOracle
from sregym.conductor.oracles.deployment_readiness import DeploymentReadinessOracle
from sregym.conductor.oracles.dev_shm_mitigation_oracle import DevShmMitigationOracle
from sregym.conductor.oracles.expired_tls_mitigation_oracle import ExpiredTlsMitigationOracle
from sregym.conductor.oracles.failure import FailureClass
from sregym.conductor.oracles.mitigation import MitigationOracle
from sregym.conductor.problems.base import Problem
from sregym.conductor.problems.incident_arena.frappe import schema_privileges, site_account, site_database
from sregym.conductor.problems.ephemeral_port_range_hotel_reservation import (
    BAD_RANGE as EPHEMERAL_BAD_RANGE,
)
from sregym.conductor.problems.ephemeral_port_range_hotel_reservation import (
    SYSCTL_NAME as EPHEMERAL_SYSCTL,
)
from sregym.conductor.problems.expired_tls_hotel_reservation import ExpiredTlsHotelReservation
from sregym.conductor.problems.lite_ia.k8s import ported
from sregym.conductor.problems.mongo_storage_faults import FAULT_POD_NAMESPACE
from sregym.conductor.problems.node_conntrack_exhaustion import NodeConntrackExhaustionHotelReservation
from sregym.conductor.problems.psa_restricted_blocks_recreation import (
    PSA_ENFORCE_LABEL,
    PSA_ENFORCE_VERSION_LABEL,
    RESTRICTED_PROFILE,
    PSARestrictedBlocksRecreation,
)
from sregym.observer.ingress_nginx import IngressNginx
from sregym.service.apps.incident_arena import Frappe
from sregym.service.runtime_images import REDIS_CLIENT_IMAGE
from sregym.utils.decorators import mark_fault_injected

APP = "frappe"
GUNICORN = "erp-gunicorn"
WEB_SERVICE = "svc-frappe-web"


# ---------------------------------------------------------------------- helpers
def selector_of(problem, deployment: str) -> str:
    """Label selector string built from a Deployment's ``spec.selector.matchLabels``."""
    dep = problem.kubectl.get_deployment(deployment, problem.namespace)
    return ",".join(f"{k}={v}" for k, v in sorted(dep.spec.selector.match_labels.items()))


def rollout_status(problem, deployment: str, timeout_s: int = 600, check: bool = True) -> str:
    command = f"kubectl rollout status deployment/{deployment} -n {problem.namespace} --timeout={timeout_s}s"
    if check:
        return problem.kubectl.exec_command_checked(command, timeout=timeout_s + 30)
    return problem.kubectl.exec_command(command)


def replace_pods_with_patch(problem, deployment: str, patch: dict, wait_ready: bool = False) -> None:
    """Apply a pod-template ``patch`` so that no pod of the old template keeps serving.

    A rolling update of a Deployment whose new pods never become Ready leaves
    the old pods serving (default surge), which hides the fault (H3). Scaling
    to zero first, patching, then scaling back makes the new template the only
    one running, as the originals' delete+apply injectors did.
    """
    ns = problem.namespace
    dep = problem.kubectl.get_deployment(deployment, ns)
    replicas = dep.spec.replicas if dep.spec.replicas is not None else 1
    selector = selector_of(problem, deployment)
    problem.kubectl.exec_command_checked(f"kubectl scale deployment/{deployment} -n {ns} --replicas=0")
    problem.kubectl.exec_command(f"kubectl wait pod -n {ns} -l {selector} --for=delete --timeout=180s")
    try:
        problem.kubectl.exec_command_checked(
            f"kubectl patch deployment {deployment} -n {ns} --type=strategic -p {shlex.quote(json.dumps(patch))}"
        )
    finally:
        problem.kubectl.exec_command_checked(f"kubectl scale deployment/{deployment} -n {ns} --replicas={replicas}")
    if wait_ready:
        rollout_status(problem, deployment)


# ---------------------------------------------------------------------- PSA restricted
class PSARestrictedBlocksRecreationFrappe(PSARestrictedBlocksRecreation):
    """``enforce: restricted`` on the Frappe namespace, then the gunicorn pod is deleted.

    The Frappe image adds ``CAP_CHOWN`` and sets no seccomp profile or
    ``runAsNonRoot``/``allowPrivilegeEscalation``, so the ReplicaSet's
    replacement pod is rejected at admission and the Desk API has no backend.
    """

    def __init__(self, app_name: str = APP, faulty_service: str = GUNICORN):
        self.faulty_service = faulty_service
        self._prior_psa_labels = {}
        ported(
            self,
            app_name,
            component=f"namespace/{APP}",
            description=(
                f"The `{APP}` namespace carries the Pod Security Admission label "
                f"`{PSA_ENFORCE_LABEL}={RESTRICTED_PROFILE}`, so the kube-apiserver enforces the restricted Pod "
                "Security Standard at admission. Frappe's pods add the `CAP_CHOWN` capability and set no seccomp "
                "profile, `runAsNonRoot` or `allowPrivilegeEscalation: false`, so they violate the restricted "
                f"profile. Running pods were unaffected, but the `{faulty_service}` pod (gunicorn behind "
                f"Service `{WEB_SERVICE}`) was deleted and its ReplicaSet's replacement is rejected with "
                '`violates PodSecurity "restricted"`, so the deployment has no ready replica and every Desk/API '
                "request fails although the Deployment spec, image and Service are healthy. No admission webhook "
                "is involved. Mitigation: remove the enforce label, relax it to a profile the workload satisfies, "
                "or make the workload compliant."
            ),
            oracle_factory=DeploymentReadinessOracle,
        )
        self.core_api = client.CoreV1Api()

    @mark_fault_injected
    def inject_fault(self):
        self._capture_prior_psa_labels()
        self._patch_namespace_labels(
            {PSA_ENFORCE_LABEL: RESTRICTED_PROFILE, PSA_ENFORCE_VERSION_LABEL: "latest"}
        )
        selector = selector_of(self, self.faulty_service)
        pods = self.core_api.list_namespaced_pod(self.namespace, label_selector=selector).items
        if not pods:
            raise RuntimeError(f"No pods found for {self.faulty_service}")
        for pod in pods:
            self.core_api.delete_namespaced_pod(
                pod.metadata.name, self.namespace, body=client.V1DeleteOptions(grace_period_seconds=0)
            )
        print(f"Labelled {self.namespace} restricted and deleted {[p.metadata.name for p in pods]}")


# ---------------------------------------------------------------------- state oracles
class FrappeFaultStateOracle(MitigationOracle):
    """The generic workload health check, then the port's own fault-state check.

    ``problem.fault_check(oracle)`` returns ``None`` when the injected fault is
    gone, or a failure verdict (built with ``oracle.fail``) describing why not.
    """

    FAILURE_CLASSES = {
        "fault_still_present": FailureClass.AGENT_ERROR,
        "site_database_unusable": FailureClass.AGENT_ERROR,
    }

    def evaluate(self) -> dict:
        result = super().evaluate()
        try:
            verdict = self.problem.fault_check(self)
        except Exception as exc:
            print(f"❌ Fault-state check raised: {exc}")
            verdict = self.fail_from_exception(exc)
        # The fault-specific verdict is the more telling reason when both fail.
        return verdict or result


SITE_DB_PROBE = """
frappe.db.sql("SELECT COUNT(*) FROM `tabDocType`")
frappe.db.sql("INSERT INTO `tabToDo` (name) SELECT name FROM `tabToDo` WHERE 1=0")
frappe.db.sql("UPDATE `tabToDo` SET modified = modified WHERE 1=0")
frappe.db.sql("DELETE FROM `tabToDo` WHERE 1=0")
frappe.db.rollback()
print("SITE_DB_OK")
"""


def site_db_probe(problem, oracle) -> dict | None:
    """Connect to MariaDB exactly as the site does (site_config credentials, host,
    transport) from the gunicorn pod and exercise SELECT/INSERT/UPDATE/DELETE
    (no-op statements, rolled back)."""
    try:
        out = problem.app.site_python(SITE_DB_PROBE, timeout=180)
    except Exception as exc:
        error = "\n".join(line for line in str(exc).splitlines() if "RuntimeWarning" not in line)[-600:]
        print(f"❌ The site cannot use its database: {error}")
        return oracle.fail("site_database_unusable", error=error)
    if "SITE_DB_OK" not in out:
        print(f"❌ Unexpected site probe output: {out[-300:]!r}")
        return oracle.fail("site_database_unusable", output=out[-300:])
    print("✅ The site connects to MariaDB with its own account and can read and write")
    return None


def site_config(problem) -> dict:
    path = f"/home/frappe/frappe-bench/sites/{problem.app.SITE_NAME}/site_config.json"
    return json.loads(problem.app.exec_in(f"deploy/{GUNICORN}", f"cat {path}"))


def delete_pods(problem, deployment: str) -> list[str]:
    selector = selector_of(problem, deployment)
    pods = problem.kubectl.core_v1_api.list_namespaced_pod(problem.namespace, label_selector=selector).items
    for pod in pods:
        problem.kubectl.core_v1_api.delete_namespaced_pod(pod.metadata.name, problem.namespace)
    return [pod.metadata.name for pod in pods]


def apply_manifest(problem, manifest: dict) -> None:
    problem.kubectl.exec_command_checked("kubectl apply -f -", input_data=json.dumps(manifest), timeout=60)


def state_file(problem, name: str) -> Path:
    return Path(f"/tmp/sregym-{problem.namespace}-{name}.json")


# ---------------------------------------------------------------------- MariaDB access faults
class _SiteAccountFault(Problem):
    """Base for faults on the Frappe site's MariaDB account (was a MongoDB user)."""

    DESCRIPTION = ""
    COMPONENT = "statefulset/frappe-mariadb-subchart"
    # Faithful to the originals, which restarted the dependent service so it reconnects into the fault.
    RESTART_CLIENTS = (GUNICORN,)

    def __init__(self, app_name: str = APP):
        self.faulty_service = "frappe-mariadb-subchart"
        self.database = ""
        self.grantee = ""
        ported(
            self,
            app_name,
            component=self.COMPONENT,
            description=self.DESCRIPTION,
            oracle_factory=FrappeFaultStateOracle,
        )

    def _resolve(self) -> None:
        if self.grantee:
            return
        try:
            self.database = site_database(self.app)
            self.grantee = site_account(self.app, self.database)
        except RuntimeError:
            # Under the fault the account may hold no privilege (or not exist): use what inject saved.
            saved = self._load()
            if not saved.get("grantee"):
                raise
            self.database, self.grantee = saved["database"], saved["grantee"]

    def _save(self, **extra) -> None:
        state_file(self, type(self).__name__).write_text(
            json.dumps({"database": self.database, "grantee": self.grantee, **extra})
        )

    def _load(self) -> dict:
        path = state_file(self, type(self).__name__)
        return json.loads(path.read_text()) if path.exists() else {}

    def _restart_clients(self) -> None:
        for deployment in self.RESTART_CLIENTS:
            print(f"Deleted {deployment} pods {delete_pods(self, deployment)} so they reconnect")

    def fault_check(self, oracle) -> dict | None:
        return site_db_probe(self, oracle)


class RevokeAuthFrappe(_SiteAccountFault):
    """``REVOKE SELECT, INSERT, UPDATE, DELETE`` on the site schema from the site account."""

    PRIVILEGES = ("SELECT", "INSERT", "UPDATE", "DELETE")
    COMPONENT = "statefulset/frappe-mariadb-subchart (grants of the Frappe site account)"
    DESCRIPTION = (
        "Database access for the Frappe site was explicitly revoked in MariaDB (`frappe-mariadb-subchart`): "
        "`REVOKE SELECT, INSERT, UPDATE, DELETE ON <site schema>.* FROM <site account>`, where the site account "
        "is the per-site user named in `sites/svc-frappe-web/site_config.json` (`db_name`). The account still "
        "exists and authenticates, so gunicorn (`erp-gunicorn`, Service `svc-frappe-web`) and the RQ workers "
        "start, but every database-backed request fails with ERROR 1142 (`SELECT command denied to user ...`), "
        "so the Desk/API requests of the site's users fail. Fix: grant the privileges back to the site account."
    )

    @mark_fault_injected
    def inject_fault(self):
        self._resolve()
        self._save()
        self.app.mysql(f"REVOKE {', '.join(self.PRIVILEGES)} ON `{self.database}`.* FROM {self.grantee}")
        held = set(schema_privileges(self.app, self.database, self.grantee))
        if held & set(self.PRIVILEGES):
            raise RuntimeError(f"{self.grantee} still holds {sorted(held & set(self.PRIVILEGES))}")
        print(f"Revoked {self.PRIVILEGES} on {self.database} from {self.grantee}")
        self._restart_clients()

    @mark_fault_injected
    def recover_fault(self):
        self._resolve()
        self.app.mysql(f"GRANT {', '.join(self.PRIVILEGES)} ON `{self.database}`.* TO {self.grantee}")
        print(f"Granted {self.PRIVILEGES} on {self.database} back to {self.grantee}")


class StorageUserUnregisteredFrappe(_SiteAccountFault):
    """``DROP USER`` of the site's MariaDB account; gunicorn is restarted into the failure."""

    COMPONENT = "statefulset/frappe-mariadb-subchart (Frappe site account)"
    DESCRIPTION = (
        "The Frappe site's MariaDB account is missing: the per-site user (named by `db_name` in "
        "`sites/svc-frappe-web/site_config.json`, with password `db_password`) was dropped from MariaDB "
        "(`frappe-mariadb-subchart`). Gunicorn (`erp-gunicorn`, Service `svc-frappe-web`) and the RQ workers "
        "cannot authenticate (`Access denied for user ...`, ERROR 1045), so every storage-backed request fails. "
        "Fix: recreate the user with the site's password and grant it all privileges on the site schema."
    )

    @mark_fault_injected
    def inject_fault(self):
        self._resolve()
        grants = [line for line in self.app.mysql(f"SHOW GRANTS FOR {self.grantee}").splitlines() if line.strip()]
        self._save(grants=grants)
        self.app.mysql(f"DROP USER {self.grantee}")
        print(f"Dropped {self.grantee} (had {len(grants)} grant lines)")
        self._restart_clients()

    @mark_fault_injected
    def recover_fault(self):
        self._resolve()
        grants = self._load().get("grants") or []
        if not grants:
            raise RuntimeError("no saved grants for the site account")
        config = site_config(self)
        password = config["db_password"].replace("'", "''")
        self.app.mysql(f"CREATE USER IF NOT EXISTS {self.grantee} IDENTIFIED BY '{password}'")
        for line in grants:
            if line.upper().startswith("GRANT USAGE ON *.*"):
                continue
            self.app.mysql(line)
        print(f"Recreated {self.grantee} with {len(grants)} grant lines")


class AuthMissFrappe(_SiteAccountFault):
    """MariaDB demands TLS (``require_secure_transport=ON``) that none of its clients use."""

    COMPONENT = "statefulset/frappe-mariadb-subchart (require_secure_transport)"
    DESCRIPTION = (
        "The Frappe MariaDB server (`frappe-mariadb-subchart`) requires secure transport: the global "
        "`require_secure_transport` was set to ON, but TLS is not set up between the server and its clients "
        "(Frappe connects over plain TCP and no client certificate/TLS configuration exists). Every TCP connection "
        "is refused at authentication (`Access denied for user '<site account>'@'<pod IP>' (using password: YES)`, "
        "ERROR 1045, although the password in site_config.json is correct), "
        "so gunicorn (`erp-gunicorn`) and the RQ workers cannot reach the database and every database-dependent "
        "request fails. Fix: turn `require_secure_transport` off (or set up working TLS for every client)."
    )

    @mark_fault_injected
    def inject_fault(self):
        self.app.mysql("SET GLOBAL require_secure_transport = ON")
        print("MariaDB now rejects non-TLS connections")
        self._restart_clients()

    @mark_fault_injected
    def recover_fault(self):
        self.app.mysql("SET GLOBAL require_secure_transport = OFF")
        print("MariaDB accepts plain-TCP clients again")


# ---------------------------------------------------------------------- ephemeral port range
class EphemeralPortRangeFrappe(Problem):
    """``net.ipv4.ip_local_port_range`` narrowed to four ports on gunicorn.

    Frappe opens a fresh MariaDB connection for every request (and closes it,
    leaving the client side in TIME_WAIT), so four ephemeral ports run out at
    once and outbound connects fail with EADDRNOTAVAIL while the pod runs.
    """

    SYSCTL = EPHEMERAL_SYSCTL
    BAD_RANGE = EPHEMERAL_BAD_RANGE
    MIN_PORTS = 1024

    def __init__(self, app_name: str = APP, faulty_service: str = GUNICORN):
        self.faulty_service = faulty_service
        ported(
            self,
            app_name,
            component=f"deployment/{faulty_service}",
            description=(
                f"The `{faulty_service}` pod template sets the namespaced sysctl `{self.SYSCTL}` to "
                f"'{self.BAD_RANGE}' in its securityContext, narrowing the ephemeral port range to 4 ports. "
                "Gunicorn opens a new TCP connection to MariaDB (and Redis) for its requests; the few local ports are "
                "quickly exhausted (closed connections sit in TIME_WAIT), so outbound connects fail with "
                "EADDRNOTAVAIL ('Cannot assign requested address') and the Desk/API requests behind "
                f"`{WEB_SERVICE}` fail, although the pod stays Running. Fix: remove the sysctl (or restore a normal range)."
            ),
            oracle_factory=FrappeFaultStateOracle,
        )

    @staticmethod
    def _sysctl_patch(value):
        sysctls = None if value is None else [{"name": EPHEMERAL_SYSCTL, "value": value}]
        return {"spec": {"template": {"spec": {"securityContext": {"sysctls": sysctls}}}}}

    @mark_fault_injected
    def inject_fault(self):
        replace_pods_with_patch(self, self.faulty_service, self._sysctl_patch(self.BAD_RANGE))
        print(f"{self.faulty_service} now runs with {self.SYSCTL}={self.BAD_RANGE}")

    @mark_fault_injected
    def recover_fault(self):
        self.kubectl.exec_command_checked(
            f"kubectl patch deployment {self.faulty_service} -n {self.namespace} --type=merge "
            f"-p {shlex.quote(json.dumps(self._sysctl_patch(None)))}"
        )
        rollout_status(self, self.faulty_service)

    def fault_check(self, oracle) -> dict | None:
        selector = selector_of(self, self.faulty_service)
        pods = self.kubectl.core_v1_api.list_namespaced_pod(self.namespace, label_selector=selector).items
        running = [p for p in pods if p.status.phase == "Running" and not p.metadata.deletion_timestamp]
        if not running:
            return oracle.fail("no_pods_found", deployment=self.faulty_service)
        for pod in running:
            value = self.app.exec_in(f"pod/{pod.metadata.name}", f"cat /proc/sys/{self.SYSCTL.replace('.', '/')}")
            low, high = (int(v) for v in value.split())
            print(f"{pod.metadata.name}: {self.SYSCTL} = {low} {high}")
            if high - low + 1 < self.MIN_PORTS:
                print(f"❌ {pod.metadata.name} has only {high - low + 1} ephemeral ports")
                return oracle.fail("fault_still_present", pod=pod.metadata.name, port_range=f"{low} {high}")
        return None


# ---------------------------------------------------------------------- configmap drift
class ConfigMapDriftFrappe(Problem):
    """gunicorn reads the site's ``site_config.json`` from a ConfigMap that has drifted.

    The site config (database name, user, password, ...) is mounted onto
    gunicorn from ConfigMap ``erp-site-config`` (subPath), and that ConfigMap
    lost ``db_password``: gunicorn's database logins are refused, so every
    request fails while the workers (reading the intact file from the shared
    volume) are fine. (Other keys are masked by fallbacks: ``db_host`` is
    repeated in the bench-wide config, ``db_name`` defaults from ``db_user``
    and a missing Redis cache URL only degrades caching.)
    """

    CONFIGMAP = "erp-site-config"
    KEY = "site_config.json"
    VOLUME = "site-config"
    MOUNT_PATH = f"/home/frappe/frappe-bench/sites/{Frappe.SITE_NAME}/site_config.json"
    DROPPED_KEYS = ("db_password",)

    def __init__(self, app_name: str = APP, faulty_service: str = GUNICORN, container: str = "gunicorn"):
        self.faulty_service = faulty_service
        self.container = container
        self.expected_keys: list[str] = []
        ported(
            self,
            app_name,
            component=f"configmap/{self.CONFIGMAP}",
            description=(
                f"ConfigMap `{self.CONFIGMAP}`, which deployment `{faulty_service}` mounts (subPath) as the Frappe "
                f"site's `sites/{Frappe.SITE_NAME}/site_config.json`, has drifted and is missing a required key "
                f"({', '.join(f'`{k}`' for k in self.DROPPED_KEYS)}, the site account's MariaDB password). Gunicorn "
                "therefore starts with incomplete runtime config: its database logins are refused (`Access denied "
                f"for user ... (using password: NO)`), so it fails readiness and every Desk/API request behind "
                f"`{WEB_SERVICE}`, while the RQ workers, which read the intact file from the shared volume, keep "
                "working. Fix: restore the missing key in the ConfigMap (and restart gunicorn, since a subPath mount "
                "is not refreshed), or stop mounting the drifted copy."
            ),
            oracle_factory=FrappeFaultStateOracle,
        )

    def _read_file(self, target: str) -> dict:
        return json.loads(self.app.exec_in(target, f"cat {self.MOUNT_PATH}", container=self.container))

    @mark_fault_injected
    def inject_fault(self):
        config = self._read_file(f"deploy/{self.faulty_service}")
        self.expected_keys = sorted(config)
        state_file(self, "configmap-drift").write_text(json.dumps(self.expected_keys))
        drifted = {k: v for k, v in config.items() if k not in self.DROPPED_KEYS}
        apply_manifest(self, 
            {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {"name": self.CONFIGMAP, "namespace": self.namespace},
                "data": {self.KEY: json.dumps(drifted, indent=1)},
            }
        )
        patch = {
            "spec": {
                "template": {
                    "spec": {
                        "volumes": [{"name": self.VOLUME, "configMap": {"name": self.CONFIGMAP}}],
                        "containers": [
                            {
                                "name": self.container,
                                "volumeMounts": [
                                    {
                                        "name": self.VOLUME,
                                        "mountPath": self.MOUNT_PATH,
                                        "subPath": self.KEY,
                                        "readOnly": True,
                                    }
                                ],
                            }
                        ],
                    }
                }
            }
        }
        replace_pods_with_patch(self, self.faulty_service, patch)
        print(f"{self.faulty_service} reads {self.MOUNT_PATH} from {self.CONFIGMAP} without {self.DROPPED_KEYS}")

    @mark_fault_injected
    def recover_fault(self):
        patch = {
            "spec": {
                "template": {
                    "spec": {
                        "volumes": [{"name": self.VOLUME, "$patch": "delete"}],
                        "containers": [
                            {
                                "name": self.container,
                                "volumeMounts": [{"mountPath": self.MOUNT_PATH, "$patch": "delete"}],
                            }
                        ],
                    }
                }
            }
        }
        self.kubectl.exec_command_checked(
            f"kubectl patch deployment {self.faulty_service} -n {self.namespace} --type=strategic "
            f"-p {shlex.quote(json.dumps(patch))}"
        )
        rollout_status(self, self.faulty_service)
        self.kubectl.exec_command(f"kubectl delete configmap {self.CONFIGMAP} -n {self.namespace} --ignore-not-found")

    def fault_check(self, oracle) -> dict | None:
        expected = self.expected_keys or json.loads(state_file(self, "configmap-drift").read_text())
        dep = self.kubectl.get_deployment(self.faulty_service, self.namespace)
        mounts_cm = any(
            v.config_map and v.config_map.name == self.CONFIGMAP for v in dep.spec.template.spec.volumes or []
        )
        if mounts_cm:
            cm = self.kubectl.core_v1_api.read_namespaced_config_map(self.CONFIGMAP, self.namespace)
            missing = [k for k in expected if k not in json.loads((cm.data or {}).get(self.KEY) or "{}")]
            if missing:
                print(f"❌ ConfigMap {self.CONFIGMAP} is still missing {missing}")
                return oracle.fail("fault_still_present", configmap=self.CONFIGMAP, missing_keys=missing)
        selector = selector_of(self, self.faulty_service)
        pods = self.kubectl.core_v1_api.list_namespaced_pod(self.namespace, label_selector=selector).items
        for pod in pods:
            if pod.status.phase != "Running" or pod.metadata.deletion_timestamp:
                continue
            missing = [k for k in expected if k not in self._read_file(f"pod/{pod.metadata.name}")]
            if missing:
                print(f"❌ {pod.metadata.name} still runs without {missing}")
                return oracle.fail("fault_still_present", pod=pod.metadata.name, missing_keys=missing)
        print("✅ gunicorn runs with the complete site_config.json")
        return None


# ---------------------------------------------------------------------- cache memory flood
class ValkeyMemoryDisruptionFrappe(Problem):
    """A Job floods Frappe's Redis cache with 1 MB values until it is OOM-killed.

    The cache (``frappe-redis-cache-master``, 192Mi limit) has no ``maxmemory``
    and keeps an append-only file on an emptyDir, so the container is OOM-killed
    and then OOM-killed again replaying its AOF while the flood continues.
    """

    JOB_NAME = "cache-memory-flood"
    CACHE = Frappe.REDIS_CACHE_STATEFULSET

    def __init__(self, app_name: str = APP):
        self.faulty_service = self.CACHE
        ported(
            self,
            app_name,
            component=f"statefulset/{self.CACHE}",
            description=(
                f"A Job (`{self.JOB_NAME}`) in the namespace floods Frappe's Redis cache (`{self.CACHE}`, Service "
                f"`{self.CACHE}`) with very large (1 MB) values from 10 threads. The cache has no `maxmemory` limit "
                "and runs with a 192Mi container memory limit, so its memory is exhausted and the container is "
                "OOMKilled and restarts (replaying the flooded append-only file), leaving the cache unavailable; "
                "gunicorn's sessions/cache lookups fail, so Desk/API requests error. Fix: stop the flood job and "
                "bring the cache back with its memory under control."
            ),
            oracle_factory=FrappeFaultStateOracle,
        )

    @mark_fault_injected
    def inject_fault(self):
        script = textwrap.dedent(
            f"""
            import redis, threading, time
            def flood():
                c = redis.Redis(host='{self.CACHE}', port=6379)
                while True:
                    try:
                        c.set(f"key_{{time.time()}}", 'x' * 1000000)
                    except Exception as e:
                        print(f"Error: {{e}}"); time.sleep(1)
            ts = [threading.Thread(target=flood) for _ in range(10)]
            [t.start() for t in ts]; [t.join() for t in ts]
            """
        ).strip()
        encoded = base64.b64encode(script.encode()).decode()
        job = {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {"name": self.JOB_NAME, "namespace": self.namespace},
            "spec": {
                "backoffLimit": 1000,
                "template": {
                    "spec": {
                        "restartPolicy": "OnFailure",
                        "containers": [
                            {
                                "name": "flooder",
                                "image": REDIS_CLIENT_IMAGE,
                                "command": ["python3", "-c", f"import base64; exec(base64.b64decode('{encoded}'))"],
                            }
                        ],
                    }
                },
            },
        }
        baseline_restarts = self._cache_restarts()
        client.BatchV1Api().create_namespaced_job(self.namespace, job)
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            restarts = self._cache_restarts()
            if restarts > baseline_restarts:
                print(f"{self.CACHE} restarted under the flood (restarts={restarts})")
                return
            time.sleep(5)
        print("⚠️ cache did not restart within 300s; proceeding")

    def _cache_restarts(self) -> int:
        pod = self.kubectl.core_v1_api.read_namespaced_pod(f"{self.CACHE}-0", self.namespace)
        return sum(cs.restart_count or 0 for cs in pod.status.container_statuses or [])

    @mark_fault_injected
    def recover_fault(self):
        self.kubectl.exec_command(
            f"kubectl delete job {self.JOB_NAME} -n {self.namespace} --cascade=foreground --ignore-not-found --wait=true"
        )
        # The flooded append-only file lives on the pod's emptyDir: a new pod starts empty.
        self.kubectl.exec_command(f"kubectl delete pod {self.CACHE}-0 -n {self.namespace} --wait=true")
        self.kubectl.exec_command_checked(
            f"kubectl rollout status statefulset/{self.CACHE} -n {self.namespace} --timeout=300s", timeout=330
        )

    def fault_check(self, oracle) -> dict | None:
        jobs = client.BatchV1Api().list_namespaced_job(self.namespace).items
        for job in jobs:
            if job.metadata.name == self.JOB_NAME and (job.status.active or 0) > 0:
                print(f"❌ Flood job {self.JOB_NAME} is still active")
                return oracle.fail("fault_still_present", job=self.JOB_NAME)
        sts = self.kubectl.apps_v1_api.read_namespaced_stateful_set(self.CACHE, self.namespace)
        if (sts.status.ready_replicas or 0) < (sts.spec.replicas or 1):
            print(f"❌ {self.CACHE} is not ready")
            return oracle.fail("fault_still_present", statefulset=self.CACHE, reason_detail="cache_not_ready")
        pong = self.app.redis("PING", cache=True).strip()
        if pong != "PONG":
            return oracle.fail("fault_still_present", statefulset=self.CACHE, ping=pong[:200])
        print(f"✅ No flood running and {self.CACHE} answers PING")
        return None


# ---------------------------------------------------------------------- /dev/shm exhaustion
class DevShmFrappeOracle(DevShmMitigationOracle):
    """``DevShmMitigationOracle`` that also accepts removing the oversized staging step.

    In the original the shared-memory need is a brand-new workload's, so only a
    bigger /dev/shm fixes it. Here it arrives as a worker template change with
    rollout history, so rolling that change back is an equally valid repair.
    """

    def _has_memory_backed_shm(self, pod_spec) -> bool:
        if super()._has_memory_backed_shm(pod_spec):
            return True
        marker = f"{self.problem.shm_mount_path}/rq-spool"
        return not any(
            marker in " ".join([*(c.command or []), *(c.args or [])]) for c in pod_spec.containers or []
        )


class DevShmExhaustionFrappe(Problem):
    """The RQ long worker stages ~80 MiB in /dev/shm before starting; the runtime default is 64 MiB."""

    shm_mount_path = "/dev/shm"
    scratch_mib = 80

    def __init__(self, app_name: str = APP, worker: str = Frappe.WORKER_DEPLOYMENT, container: str = "long"):
        self.worker_name = self.faulty_service = worker
        self.container = container
        ported(
            self,
            app_name,
            component=f"deployment/{worker}",
            description=(
                f"The `{worker}` deployment (Frappe's RQ worker for the long/default/short queues) stages about "
                f"{self.scratch_mib} MiB of scratch data in {self.shm_mount_path} when it starts, but its pod template "
                f"does not mount a memory-backed emptyDir (medium: Memory) at {self.shm_mount_path}. The container "
                "therefore gets the container runtime's default 64 MiB /dev/shm tmpfs; the write fails with ENOSPC "
                '("No space left on device"), the container exits non-zero and the deployment is in '
                "CrashLoopBackOff although the node has ample disk, so background jobs are no longer processed: the "
                "RQ queues back up until Frappe refuses new jobs (`Too many queued background jobs`, HTTP 503). "
                f"Fix: mount an emptyDir with medium: Memory at {self.shm_mount_path} (or roll back the worker "
                "template change that added the staging step)."
            ),
            oracle_factory=lambda problem: DevShmFrappeOracle(problem),
        )
        # Graded on the worker alone, as the original is: the RQ backlog the outage
        # leaves does not drain by itself, so app health could fail a correct fix.
        self.mitigation_oracle = self.mitigation_oracle.oracles["fault"]

    @property
    def worker_pod_selector(self) -> str:
        return selector_of(self, self.worker_name)

    def _container(self, deployment):
        return next(c for c in deployment.spec.template.spec.containers if c.name == self.container)

    @mark_fault_injected
    def inject_fault(self):
        dep = self.kubectl.get_deployment(self.worker_name, self.namespace)
        args = list(self._container(dep).args or [])
        stage = (
            f"set -e\n# Stage the worker's scratch spool in shared memory before starting.\n"
            f"dd if=/dev/zero of={self.shm_mount_path}/rq-spool bs=1M count={self.scratch_mib}\n"
            f"rm -f {self.shm_mount_path}/rq-spool\nset +e\n"
        )
        if args and "Stage the worker's scratch spool" in args[0]:
            new_args = args  # Already staged (a previous run); keep it once.
        else:
            new_args = [stage + args[0]] + args[1:] if args else [stage]
        container = {"name": self.container, "args": new_args}
        spec = {"containers": [container]}
        # A memory-backed /dev/shm left by an earlier repair would absorb the fault.
        mounts = [m for m in self._container(dep).volume_mounts or [] if m.mount_path == self.shm_mount_path]
        if mounts:
            container["volumeMounts"] = [{"mountPath": self.shm_mount_path, "$patch": "delete"}]
            spec["volumes"] = [{"name": m.name, "$patch": "delete"} for m in mounts]
        patch = {"spec": {"template": {"spec": spec}}}
        replace_pods_with_patch(self, self.worker_name, patch)
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            pods = self.kubectl.core_v1_api.list_namespaced_pod(
                self.namespace, label_selector=self.worker_pod_selector
            ).items
            if any((cs.restart_count or 0) >= 1 for p in pods for cs in p.status.container_statuses or []):
                print(f"{self.worker_name} is crash-looping on /dev/shm ENOSPC")
                return
            time.sleep(5)
        print("⚠️ worker did not visibly crash within 180s")

    @mark_fault_injected
    def recover_fault(self):
        # The fix: a memory-backed /dev/shm (the staging step then fits).
        patch = {
            "spec": {
                "template": {
                    "spec": {
                        "volumes": [{"name": "dshm", "emptyDir": {"medium": "Memory", "sizeLimit": "128Mi"}}],
                        "containers": [
                            {"name": self.container, "volumeMounts": [{"name": "dshm", "mountPath": "/dev/shm"}]}
                        ],
                    }
                }
            }
        }
        self.kubectl.exec_command_checked(
            f"kubectl patch deployment {self.worker_name} -n {self.namespace} --type=strategic "
            f"-p {shlex.quote(json.dumps(patch))}"
        )
        rollout_status(self, self.worker_name)


# ---------------------------------------------------------------------- node conntrack exhaustion
class NodeConntrackExhaustionFrappe(NodeConntrackExhaustionHotelReservation):
    """Conntrack flood pinned to the node that runs gunicorn (and, via the RWO volume, the bench)."""

    def __init__(self, app_name: str = APP, frontend: str = GUNICORN):
        self.frontend_deployment = frontend
        self.victim_node = self.gateway_node = None
        self.original_conntrack_max = self.target_connections = None
        self.conntrack_max_changed = False
        ported(
            self,
            app_name,
            component=(
                f"victim node nf_conntrack table + deployment/{self.client_deployment} + "
                f"deployment/{self.gateway_deployment}"
            ),
            description=(
                f"The `{self.client_deployment}` deployment is pinned to the worker node that runs `{frontend}` (the "
                f"Frappe web tier behind `{WEB_SERVICE}`) and opens many held TCP connections to the "
                f"`{self.gateway_deployment}` service/deployment. They saturate that node's Linux nf_conntrack table, "
                "so new connections to and from the node (gunicorn's per-request MariaDB/Redis connections and the "
                "users' requests) time out or are dropped, even though Deployments, Pods, Services and Endpoints "
                "look healthy."
            ),
            oracle_factory=lambda problem: ConntrackMitigationOracle(
                problem, probe_url=f"http://{WEB_SERVICE}:8000/api/method/ping"
            ),
        )
        self.apps_v1 = client.AppsV1Api()
        self.core_v1 = client.CoreV1Api()

    def _frontend_node(self):
        pods = self.core_v1.list_namespaced_pod(
            self.namespace, label_selector=selector_of(self, self.frontend_deployment)
        ).items
        for pod in pods:
            if pod.status.phase == "Running" and pod.spec.node_name:
                return pod.spec.node_name

# ---------------------------------------------------------------------- silent data corruption
class SilentDataCorruptionFrappe(Problem):
    """Overwrite InnoDB pages of Frappe's MariaDB volume with random bytes.

    As in the MongoDB original, the database is stopped, its files are damaged
    on the node (keeping each file's first page so the files look intact), and
    it is started again: InnoDB fails page checksums and mysqld aborts on every
    start. A ``bench backup`` taken just before (in the site's backups folder)
    is the realistic way back; the harness also snapshots the data directory on
    the node so recovery can restore it without reinstalling the site.
    """

    STS = Frappe.MARIADB_STATEFULSET
    SNAPSHOT_ROOT = "/var/lib/sregym-snapshots"

    def __init__(self, app_name: str = APP):
        self.faulty_service = self.STS
        ported(
            self,
            app_name,
            component=f"statefulset/{self.STS}",
            description=(
                f"The data files on the persistent volume backing Frappe's MariaDB (`{self.STS}`) were silently "
                "corrupted at the storage layer, without any I/O errors being reported: InnoDB pages of the system "
                "tablespace (`ibdata1`) and of the site schema's `.ibd` files contain garbage. InnoDB detects the "
                "damage as page checksum failures when it reads them and mysqld aborts during startup on every "
                "start, so the database is in CrashLoopBackOff and every request of the Frappe site fails. The data "
                "must be restored (e.g. reinitialize the database and restore the site's latest `bench backup` from "
                "`sites/svc-frappe-web/private/backups`)."
            ),
            oracle_factory=FrappeFaultStateOracle,
        )

    def _volume_location(self) -> tuple[str, str]:
        core = self.kubectl.core_v1_api
        claim = f"data-{self.STS}-0"
        pvc = core.read_namespaced_persistent_volume_claim(claim, self.namespace)
        pv = core.read_persistent_volume(pvc.spec.volume_name)
        path = pv.spec.local.path if pv.spec.local else pv.spec.host_path.path
        node = None
        terms = pv.spec.node_affinity.required.node_selector_terms if pv.spec.node_affinity else []
        for term in terms or []:
            for expr in term.match_expressions or []:
                if expr.key == "kubernetes.io/hostname" and expr.values:
                    node = expr.values[0]
        if node is None:
            node = core.read_namespaced_pod(f"{self.STS}-0", self.namespace).spec.node_name
        return node, path

    def _scale(self, replicas: int) -> None:
        self.kubectl.exec_command_checked(f"kubectl scale statefulset/{self.STS} -n {self.namespace} --replicas={replicas}")
        if replicas == 0:
            self.kubectl.exec_command(
                f"kubectl wait pod/{self.STS}-0 -n {self.namespace} --for=delete --timeout=180s"
            )

    def _node_script(self, node: str, script: str) -> str:
        self.kubectl.create_namespace_if_not_exist(FAULT_POD_NAMESPACE)
        return self.kubectl.run_node_script_pod(
            node_name=node, namespace=FAULT_POD_NAMESPACE, script=script, name_prefix="sregym-storage-fault",
            timeout=600,
        )

    @mark_fault_injected
    def inject_fault(self):
        database = site_database(self.app)
        try:
            out = self.app.exec_in(
                f"deploy/{GUNICORN}", f"cd /home/frappe/frappe-bench && bench --site {self.app.SITE_NAME} backup",
                timeout=600,
            )
            print(out.strip().splitlines()[-1] if out.strip() else "bench backup done")
        except Exception as exc:  # The fault does not depend on it.
            print(f"⚠️ bench backup failed: {exc}")
        node, path = self._volume_location()
        snapshot = f"{self.SNAPSHOT_ROOT}/{self.namespace}-{self.STS}"
        state_file(self, "silent-corruption").write_text(json.dumps({"node": node, "path": path}))
        self._scale(0)
        script = f"""set -e
cd "/host{path}"
rm -rf "/host{snapshot}"; mkdir -p "/host{snapshot}"
cp -a . "/host{snapshot}/"
cd data
for f in ibdata1 {database}/*.ibd; do
    size=$(stat -c %s "$f")
    [ "$size" -gt 32768 ] || continue
    dd if=/dev/urandom of="$f" bs=16384 seek=1 count=$((size / 16384 - 1)) conv=notrunc 2>/dev/null
done
echo corrupted
"""
        print(self._node_script(node, script).strip())
        self._scale(1)
        print(f"Corrupted InnoDB pages of {self.STS} on {node}:{path}")

    @mark_fault_injected
    def recover_fault(self):
        saved = json.loads(state_file(self, "silent-corruption").read_text())
        node, path = saved["node"], saved["path"]
        snapshot = f"{self.SNAPSHOT_ROOT}/{self.namespace}-{self.STS}"
        self._scale(0)
        script = f"""set -e
test -d "/host{snapshot}/data"
cd "/host{path}"
find . -mindepth 1 -maxdepth 1 -exec rm -rf {{}} +
cp -a "/host{snapshot}/." .
rm -rf "/host{snapshot}"
echo restored
"""
        print(self._node_script(node, script).strip())
        self._scale(1)
        self.kubectl.exec_command_checked(
            f"kubectl rollout status statefulset/{self.STS} -n {self.namespace} --timeout=600s", timeout=630
        )

    def fault_check(self, oracle) -> dict | None:
        sts = self.kubectl.apps_v1_api.read_namespaced_stateful_set(self.STS, self.namespace)
        if (sts.status.ready_replicas or 0) < 1:
            print(f"❌ {self.STS} is not ready")
            return oracle.fail("fault_still_present", statefulset=self.STS)
        return site_db_probe(self, oracle)


# ---------------------------------------------------------------------- homepage flood
class LoadGeneratorFloodHomepageFrappe(Problem):
    """An in-namespace traffic client whose feature flag is flipped to flood the web tier.

    The original flipped ``loadGeneratorFloodHomepage`` in the demo's flagd
    ConfigMap so its load generator hammered the frontend's homepage. Frappe's
    own load generator cannot be reconfigured that way (it is the measurement),
    so the port runs a site warm-up client (``erp-site-warmer``) that reads the
    same kind of flag file from ConfigMap ``erp-site-warmer-flags``: off, it
    fetches the homepage once every 30 s; on, it floods it with concurrent
    requests and saturates gunicorn's 8 worker threads.
    """

    DEPLOYMENT = "erp-site-warmer"
    CONFIGMAP = "erp-site-warmer-flags"
    FLAG = "floodHomepage"
    CONCURRENCY = 320
    IMAGE = "busybox:1.36"

    def __init__(self, app_name: str = APP):
        self.faulty_service = WEB_SERVICE
        ported(
            self,
            app_name,
            component=f"deployment/{self.DEPLOYMENT} (configmap/{self.CONFIGMAP} flag {self.FLAG})",
            description=(
                f"The web tier (`{GUNICORN}`, Service `{WEB_SERVICE}`) is saturated by a sustained traffic surge on the "
                "homepage route, so users' Desk/API requests queue behind it and time out or fail. Mechanism: the "
                f"`{self.CONFIGMAP}` ConfigMap has the `{self.FLAG}` feature flag's `defaultVariant` set to `\"on\"`, "
                f"which makes the in-namespace `{self.DEPLOYMENT}` client switch from one homepage fetch every 30 s to "
                f"{self.CONCURRENCY} concurrent request loops against `http://{WEB_SERVICE}:8000/`. Fix: turn the flag "
                "off (or stop the client)."
            ),
            oracle_factory=FrappeFaultStateOracle,
        )

    def _flags(self, variant: str) -> str:
        return json.dumps(
            {
                "flags": {
                    self.FLAG: {
                        "description": "Hammer the homepage to warm every cache layer",
                        "state": "ENABLED",
                        "variants": {"on": True, "off": False},
                        "defaultVariant": variant,
                    }
                }
            },
            indent=2,
        )

    def _client(self) -> dict:
        script = textwrap.dedent(
            f"""\
            flag_on() {{ tr -d ' \\n' < /flags/flags.json | grep -o '"{self.FLAG}":.*' | grep -q '"defaultVariant":"on"'; }}
            hit() {{ wget -q -T 60 -O /dev/null "$TARGET" 2>/dev/null; }}
            while true; do
              if flag_on; then
                for i in $(seq 1 {self.CONCURRENCY}); do
                  ( while flag_on; do for j in 1 2 3 4 5 6 7 8 9 10; do hit; done; done ) &
                done
                wait
              else
                hit; sleep 30
              fi
            done
            """
        )
        labels = {"app.kubernetes.io/name": self.DEPLOYMENT, "app.kubernetes.io/component": "site-warmer"}
        return {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": self.DEPLOYMENT, "namespace": self.namespace, "labels": labels},
            "spec": {
                "replicas": 1,
                "selector": {"matchLabels": labels},
                "template": {
                    "metadata": {"labels": labels},
                    "spec": {
                        "automountServiceAccountToken": False,
                        "terminationGracePeriodSeconds": 1,
                        "containers": [
                            {
                                "name": "warmer",
                                "image": self.IMAGE,
                                "command": ["sh", "-c", script],
                                "env": [{"name": "TARGET", "value": f"http://{WEB_SERVICE}:8000/"}],
                                "volumeMounts": [{"name": "flags", "mountPath": "/flags"}],
                                "resources": {"requests": {"cpu": "100m", "memory": "256Mi"}, "limits": {"memory": "768Mi"}},
                            }
                        ],
                        "volumes": [{"name": "flags", "configMap": {"name": self.CONFIGMAP}}],
                    },
                },
            },
        }

    def _set_flag(self, variant: str) -> None:
        apply_manifest(self, 
            {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {"name": self.CONFIGMAP, "namespace": self.namespace},
                "data": {"flags.json": self._flags(variant)},
            }
        )

    @mark_fault_injected
    def inject_fault(self):
        self._set_flag("on")
        apply_manifest(self, self._client())
        rollout_status(self, self.DEPLOYMENT, timeout_s=300)
        print(f"{self.DEPLOYMENT} floods {WEB_SERVICE} ({self.FLAG}=on)")

    @mark_fault_injected
    def recover_fault(self):
        self.kubectl.exec_command(f"kubectl delete deployment {self.DEPLOYMENT} -n {self.namespace} --ignore-not-found --wait=true")
        self.kubectl.exec_command(f"kubectl delete configmap {self.CONFIGMAP} -n {self.namespace} --ignore-not-found")

    def fault_check(self, oracle) -> dict | None:
        try:
            dep = self.kubectl.apps_v1_api.read_namespaced_deployment(self.DEPLOYMENT, self.namespace)
        except client.exceptions.ApiException as exc:
            if exc.status == 404:
                print(f"✅ {self.DEPLOYMENT} is gone")
                return None
            raise
        if not (dep.spec.replicas or 0):
            print(f"✅ {self.DEPLOYMENT} is scaled to 0")
            return None
        try:
            cm = self.kubectl.core_v1_api.read_namespaced_config_map(self.CONFIGMAP, self.namespace)
            flags = json.loads((cm.data or {}).get("flags.json") or "{}")
            variant = flags.get("flags", {}).get(self.FLAG, {}).get("defaultVariant")
        except client.exceptions.ApiException as exc:
            if exc.status != 404:
                raise
            variant = None
        if variant == "on":
            print(f"❌ {self.FLAG} is still on and {self.DEPLOYMENT} is running")
            return oracle.fail("fault_still_present", configmap=self.CONFIGMAP, flag=self.FLAG)
        print(f"✅ {self.FLAG} is {variant!r}")
        return None


# ---------------------------------------------------------------------- expired TLS
class ExpiredTlsFrappe(ExpiredTlsHotelReservation):
    """The Frappe Ingress (to the ``erp`` nginx edge) serves an expired certificate.

    Like the original, the fault lives at the Ingress: pods stay healthy, and
    the chart's load generator talks to ``svc-frappe-web`` inside the cluster
    (as Hotel Reservation's wrk2 hit the frontend Service), so the load
    generator does not see it; HTTPS clients of the Ingress host do.
    """

    HOST = "erp.frappe.local"

    def __init__(self, app_name: str = APP):
        self.problem_id = "expired_tls_frappe"
        self.secret_name = "erp-tls"
        self.ingress_name = "erp-ingress"
        self.faulty_service = ["erp"]
        ported(
            self,
            app_name,
            component=self.ingress_name,
            description=(
                f"The Frappe Ingress `{self.ingress_name}` (host `{self.HOST}`, backend Service `erp`, the nginx "
                f"edge in front of gunicorn) is configured with TLS secret `{self.secret_name}`, which contains an "
                "expired certificate, so HTTPS connections to the site through the ingress controller fail "
                "certificate validation. Pods, Services and endpoints are all healthy. Fix: replace the secret with "
                "a valid certificate (or remove the TLS reference)."
            ),
            oracle_factory=lambda problem: ExpiredTlsWithProbeOracle(problem),
        )

    def _ingress(self) -> dict:
        return {
            "apiVersion": "networking.k8s.io/v1",
            "kind": "Ingress",
            "metadata": {
                "name": self.ingress_name,
                "namespace": self.namespace,
                "annotations": {
                    "nginx.ingress.kubernetes.io/ssl-redirect": "true",
                    # Frappe picks the site by Host header.
                    "nginx.ingress.kubernetes.io/upstream-vhost": WEB_SERVICE,
                },
            },
            "spec": {
                "ingressClassName": "nginx",
                "tls": [{"hosts": [self.HOST], "secretName": self.secret_name}],
                "rules": [
                    {
                        "host": self.HOST,
                        "http": {
                            "paths": [
                                {
                                    "path": "/",
                                    "pathType": "Prefix",
                                    "backend": {"service": {"name": "erp", "port": {"number": 8080}}},
                                }
                            ]
                        },
                    }
                ],
            },
        }

    @mark_fault_injected
    def inject_fault(self):
        IngressNginx().deploy()
        cert_pem, key_pem = self._generate_expired_cert_for(self.HOST)
        self._create_tls_secret(cert_pem, key_pem)
        apply_manifest(self, self._ingress())
        print(f"Ingress {self.ingress_name} serves an expired certificate for {self.HOST}")

    @mark_fault_injected
    def recover_fault(self):
        self.kubectl.exec_command(f"kubectl delete ingress {self.ingress_name} -n {self.namespace} --ignore-not-found")
        self.kubectl.exec_command(f"kubectl delete secret {self.secret_name} -n {self.namespace} --ignore-not-found")

    @staticmethod
    def _generate_expired_cert_for(host: str):
        import datetime

        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host)])
        now = datetime.datetime.now(datetime.UTC)
        cert = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=10))
            .not_valid_after(now - datetime.timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(host)]), critical=False)
            .sign(key, hashes.SHA256())
        )
        return (
            cert.public_bytes(serialization.Encoding.PEM),
            key.private_bytes(
                serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()
            ),
        )


class ExpiredTlsWithProbeOracle(ExpiredTlsMitigationOracle):
    """The original state check, then (if the Ingress still has TLS) an HTTPS probe through the controller:
    the certificate it serves for the host must not be expired and the site must answer."""

    CONTROLLER = "ingress-nginx-controller.ingress-nginx.svc.cluster.local"

    def evaluate(self) -> dict:
        result = super().evaluate()
        if not result.get("success"):
            return result
        problem = self.problem
        try:
            ingress = client.NetworkingV1Api().read_namespaced_ingress(problem.ingress_name, problem.namespace)
        except client.exceptions.ApiException as exc:
            if exc.status == 404:
                return result
            raise
        if not ingress.spec.tls:
            return result
        try:
            status, not_after, expired = self.https_probe()
        except Exception as exc:
            print(f"❌ HTTPS probe through the ingress failed: {str(exc)[-300:]}")
            return self.fail("fault_still_present", probe_error=str(exc)[-300:])
        if expired or status != "200":
            print(f"❌ HTTPS through the ingress: status={status} notAfter={not_after} expired={expired}")
            return self.fail("fault_still_present", status=status, not_after=not_after)
        print(f"✅ HTTPS through the ingress: status={status}, certificate valid until {not_after}")
        return result

    def https_probe(self) -> tuple[str, str, bool]:
        """GET /api/method/ping over HTTPS via the controller (from the toolbox); returns
        (HTTP status, certificate notAfter, whether that certificate has expired)."""
        host = ExpiredTlsFrappe.HOST
        script = (
            "import socket, ssl, subprocess\n"
            f"host, addr = {host!r}, {self.CONTROLLER!r}\n"
            "ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE\n"
            "s = ctx.wrap_socket(socket.create_connection((addr, 443), timeout=10), server_hostname=host)\n"
            "der = s.getpeercert(binary_form=True)\n"
            "s.sendall(f'GET /api/method/ping HTTP/1.1\\r\\nHost: {host}\\r\\nConnection: close\\r\\n\\r\\n'.encode())\n"
            "status = s.recv(64).split(b' ')[1].decode()\n"
            "pem = ssl.DER_cert_to_PEM_cert(der)\n"
            "end = subprocess.run(['openssl', 'x509', '-noout', '-enddate'], input=pem, capture_output=True, text=True)\n"
            "print('STATUS', status, end.stdout.strip())\n"
        )
        out = self.problem.app.toolbox_exec("python3 -", input_data=script, timeout=60)
        parts = out.split()
        status = parts[1] if len(parts) > 1 else ""
        not_after = out.split("notAfter=", 1)[1].strip() if "notAfter=" in out else ""
        expired = True
        if not_after:
            expiry = datetime.datetime.strptime(not_after, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=datetime.UTC)
            expired = expiry <= datetime.datetime.now(datetime.UTC)
        return status, not_after, expired


# Original registry id -> (ported problem id, problem class). Variants of one
# fault on different original apps map to the same port.
PORTS: dict[str, tuple[str, type]] = {
    "revoke_auth_mongodb-1": ("revoke_auth_frappe", RevokeAuthFrappe),
    "storage_user_unregistered-1": ("storage_user_unregistered_frappe", StorageUserUnregisteredFrappe),
    "auth_miss_mongodb": ("auth_miss_mongodb_frappe", AuthMissFrappe),
    "ephemeral_port_range_hotel_reservation": ("ephemeral_port_range_frappe", EphemeralPortRangeFrappe),
    "configmap_drift_hotel_reservation": ("configmap_drift_frappe", ConfigMapDriftFrappe),
    "valkey_memory_disruption": ("valkey_memory_disruption_frappe", ValkeyMemoryDisruptionFrappe),
    "dev_shm_exhaustion_hotel_reservation": ("dev_shm_exhaustion_frappe", DevShmExhaustionFrappe),
    "node_conntrack_exhaustion_hotel_reservation": (
        "node_conntrack_exhaustion_frappe",
        NodeConntrackExhaustionFrappe,
    ),
    "silent_data_corruption": ("silent_data_corruption_frappe", SilentDataCorruptionFrappe),
    "loadgenerator_flood_homepage": ("loadgenerator_flood_homepage_frappe", LoadGeneratorFloodHomepageFrappe),
    "expired_tls_hotel_reservation": ("expired_tls_frappe", ExpiredTlsFrappe),
    "psa_restricted_blocks_recreation_hotel_reservation": (
        "psa_restricted_blocks_recreation_frappe",
        PSARestrictedBlocksRecreationFrappe,
    ),
}

# Original ids in this module's share with no faithful Frappe port (for the lead to merge into NOT_PORTED).
NOT_PORTED = {
    "misconfig_app_hotel_res": (
        "The fault is a purpose-built application image whose baked-in configuration points at the wrong database "
        "port (ghcr.io/sregym/hotel-reservation misconfig tag). Frappe reads its database host/port from the site "
        "config on the shared `erp` volume, not from the image, so the same mechanism needs a rebuilt and published "
        "frappe/erpnext image with a modified default; changing the config instead is configmap_drift, and a different "
        "public image tag is incorrect_image."
    ),
}
