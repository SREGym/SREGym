"""Frappe incidents ported from Incident Arena (tasks 000-005).

Every Frappe incident pairs a MariaDB leg (a revoked site-account privilege, a
per-account connection cap, or a server-wide read-only lock) with a Redis queue
leg (a dequeue ACL denial, an undersized memory cap, or a replica write guard
on a standalone broker), so background work and foreground writes fail for
different reasons at once.
"""

from __future__ import annotations

import json
import re
import shlex
import time
from collections.abc import Callable
from pathlib import Path

from sregym.conductor.oracles.incident_arena import CheckResult
from sregym.conductor.problems.incident_arena.base import FaultLeg, IncidentArenaProblem
from sregym.service.apps.incident_arena import Frappe

MIB = 1024 * 1024
# Incident Arena: a finite, loss-intolerant queue cap between 16 and 128 MiB.
MAXMEMORY_BAND = (16 * MIB, 128 * MIB)
HEALTHY_QUEUE_FLAGS = ["--maxmemory", "64mb", "--maxmemory-policy", "noeviction"]
DEQUEUE_COMMANDS = ("blpop", "blmove", "brpop")

# Globals Incident Arena pins as stable unless a leg owns them.
PROTECTED_GLOBALS = (
    "max_connections",
    "max_user_connections",
    "wait_timeout",
    "read_only",
    "performance_schema",
    "init_connect",
    "max_statement_time",
    "innodb_buffer_pool_size",
    "transaction_isolation",
    "max_allowed_packet",
    "sql_mode",
)

SYSTEM_SCHEMAS = "('information_schema','mysql','performance_schema','sys')"


# ---------------------------------------------------------------------- MariaDB helpers
def site_database(app: Frappe) -> str:
    db = app.mysql(
        "SELECT TABLE_SCHEMA FROM information_schema.TABLES WHERE TABLE_NAME = 'tabDocType' "
        f"AND TABLE_SCHEMA NOT IN {SYSTEM_SCHEMAS} LIMIT 1"
    ).strip()
    if not db or "`" in db:
        raise RuntimeError(f"could not identify the Frappe site database (got {db!r})")
    return db


def site_account(app: Frappe, database: str) -> str:
    """The non-root account holding SELECT on the site schema, e.g. ``'_5e5..'@'%'``."""
    grantee = app.mysql(
        "SELECT GRANTEE FROM information_schema.SCHEMA_PRIVILEGES "
        f"WHERE TABLE_SCHEMA = '{database}' AND PRIVILEGE_TYPE = 'SELECT' "
        "AND GRANTEE NOT LIKE '''root''@%' LIMIT 1"
    ).strip()
    if not grantee:
        raise RuntimeError(f"no site account holds SELECT on {database}")
    return grantee


def schema_privileges(app: Frappe, database: str, grantee: str) -> list[str]:
    rows = app.mysql(
        "SELECT PRIVILEGE_TYPE FROM information_schema.SCHEMA_PRIVILEGES "
        f"WHERE TABLE_SCHEMA = '{database}' AND GRANTEE = {_sql_str(grantee)} ORDER BY 1"
    )
    return [r for r in rows.splitlines() if r]


def grant_fingerprint(app: Frappe) -> list[str]:
    """Every global/schema/table privilege row on the server, ``|``-separated.

    (``mysql -B`` escapes TABs inside a column, so a single-column row cannot
    use them as a separator.)
    """
    rows = app.mysql(
        "SELECT CONCAT_WS('|', 'user', GRANTEE, PRIVILEGE_TYPE, IS_GRANTABLE) FROM information_schema.USER_PRIVILEGES "
        "UNION ALL SELECT CONCAT_WS('|', 'schema', GRANTEE, TABLE_SCHEMA, PRIVILEGE_TYPE, IS_GRANTABLE) "
        "FROM information_schema.SCHEMA_PRIVILEGES "
        "UNION ALL SELECT CONCAT_WS('|', 'table', GRANTEE, CONCAT(TABLE_SCHEMA, '.', TABLE_NAME), PRIVILEGE_TYPE) "
        "FROM information_schema.TABLE_PRIVILEGES"
    )
    return sorted(r for r in rows.splitlines() if r)


def global_variables(app: Frappe, names) -> dict[str, str]:
    in_list = ",".join(_sql_str(n) for n in names)
    rows = app.mysql(
        f"SELECT VARIABLE_NAME, VARIABLE_VALUE FROM information_schema.GLOBAL_VARIABLES WHERE VARIABLE_NAME IN ({in_list.upper()})"
    )
    out = {}
    for row in rows.splitlines():
        name, _, value = row.partition("\t")
        if name:
            out[name.lower()] = value
    return out


def _sql_str(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "''") + "'"


# ---------------------------------------------------------------------- Redis helpers
def redis_config(app: Frappe, key: str, cache: bool = False) -> str:
    lines = app.redis("CONFIG", "GET", key, cache=cache).strip().splitlines()
    return lines[1].strip() if len(lines) >= 2 else ""


def redis_acl_rules(app: Frappe, user: str = "default") -> list[str]:
    """Command rules of a Redis ACL user, e.g. ``['+@all', '-blpop']``."""
    out = app.redis("ACL", "GETUSER", user).strip().splitlines()
    for index, line in enumerate(out):
        if line.strip() == "commands" and index + 1 < len(out):
            return out[index + 1].strip().split()
    return []


def render_start_script(script: str, flags: list[str]) -> str:
    """Replace the extraFlags lines of the Bitnami ``start-master.sh`` with ``flags``.

    The chart renders each extra flag as ``ARGS+=("<flag>")`` between the
    ``--include .../master.conf`` line and ``exec redis-server``.
    """
    lines = script.splitlines()
    try:
        start = next(i for i, line in enumerate(lines) if 'master.conf")' in line and line.startswith("ARGS+="))
        end = next(i for i, line in enumerate(lines) if line.startswith("exec redis-server"))
    except StopIteration as exc:
        raise RuntimeError("unrecognised Bitnami redis start-master.sh layout") from exc
    rendered = ['ARGS+=("' + flag.replace("\\", "\\\\").replace('"', '\\"') + '")' for flag in flags]
    return "\n".join([*lines[: start + 1], *rendered, *lines[end:]]) + "\n"


class FrappeLeg(FaultLeg):
    @property
    def frappe(self) -> Frappe:
        return self.app


# ---------------------------------------------------------------------- MariaDB legs
class SiteGrantRevocation(FrappeLeg):
    """``REVOKE <privilege> ON <site schema>.* FROM <site account>``.

    Frappe connects as a per-site account; without the privilege every write of
    that kind fails with ERROR 1142 while reads keep working, which looks like a
    storage or read-only problem from the outside.
    """

    component = "mariadb/mariadb.grants"

    def __init__(self, *privileges: str) -> None:
        super().__init__()
        self.privileges = [p.upper() for p in privileges]
        self.database = ""
        self.grantee = ""
        self.healthy_privileges: list[str] = []

    def _resolve(self) -> None:
        if not self.database:
            self.database = site_database(self.frappe)
            self.grantee = site_account(self.frappe, self.database)

    def capture_baseline(self) -> None:
        self._resolve()
        self.healthy_privileges = schema_privileges(self.frappe, self.database, self.grantee)

    def inject(self) -> None:
        self._resolve()
        self.frappe.mysql(f"REVOKE {', '.join(self.privileges)} ON `{self.database}`.* FROM {self.grantee}")
        held = set(schema_privileges(self.frappe, self.database, self.grantee))
        if held & set(self.privileges):
            raise RuntimeError(f"{self.grantee} still holds {sorted(held & set(self.privileges))}")

    def recover(self) -> None:
        self._resolve()
        self.frappe.mysql(f"GRANT {', '.join(self.privileges)} ON `{self.database}`.* TO {self.grantee}")

    def checks(self, phase: str) -> list[CheckResult]:
        self._resolve()
        held = schema_privileges(self.frappe, self.database, self.grantee)
        results = [
            CheckResult(
                f"site_account_{p.lower()}_privilege@{phase}",
                p in held,
                detail={"grantee": self.grantee, "schema": self.database, "held": held},
            )
            for p in self.privileges
        ]
        if self.healthy_privileges:
            results.append(
                CheckResult(
                    f"site_account_grants_targeted@{phase}",
                    sorted(held) == sorted(self.healthy_privileges),
                    reason="unsafe_repair",
                    detail={"expected": self.healthy_privileges, "held": held},
                )
            )
        return results

    def owned_grantee(self) -> str:
        return self.grantee

    def describe(self) -> str:
        return (
            f"REVOKE {', '.join(self.privileges)} ON <site schema>.* FROM the Frappe site account "
            f"(the per-site MariaDB user); writes needing {', '.join(self.privileges)} fail with ERROR 1142."
        )


class MariaDBGlobal(FrappeLeg):
    """``SET GLOBAL <variable> = <value>`` applied after the site is serving."""

    def __init__(
        self,
        variable: str,
        fault_value: str,
        accept: Callable[[str], bool],
        component: str,
        startup_flags: str | None = None,
    ) -> None:
        super().__init__()
        self.variable = variable
        self.fault_value = fault_value
        self.accept = accept
        self.component = component
        self.startup_flags = startup_flags
        self.healthy_value: str | None = None

    def deploy_values(self):
        if not self.startup_flags:
            return {}
        # MariaDB only allows changing some limits at runtime if they were not 0 at startup.
        return {"erpnext": {"mariadb-subchart": {"primary": {"extraFlags": self.startup_flags}}}}

    def _value(self) -> str:
        return global_variables(self.frappe, [self.variable]).get(self.variable, "")

    def capture_baseline(self) -> None:
        self.healthy_value = self._value()

    def inject(self) -> None:
        self.frappe.mysql(f"SET GLOBAL {self.variable} = {self.fault_value}")

    def recover(self) -> None:
        value = self.healthy_value if self.healthy_value not in (None, "") else "DEFAULT"
        if value in ("ON", "OFF"):
            value = "1" if value == "ON" else "0"
        self.frappe.mysql(f"SET GLOBAL {self.variable} = {value}")

    def checks(self, phase: str) -> list[CheckResult]:
        value = self._value()
        return [CheckResult(f"{self.variable}_repaired@{phase}", self.accept(value), detail={"value": value})]

    def describe(self) -> str:
        return f"SET GLOBAL {self.variable} = {self.fault_value} on MariaDB (runtime only, no my.cnf change)."


def unlimited_or_at_least(minimum: int) -> Callable[[str], bool]:
    def accept(value: str) -> bool:
        try:
            number = int(value)
        except ValueError:
            return False
        return number == 0 or number >= minimum

    return accept


def is_off(value: str) -> bool:
    return value.upper() in ("OFF", "0")


# ---------------------------------------------------------------------- Redis queue leg
class RedisQueueFlags(FrappeLeg):
    """Bake faulty flags into the queue broker's startup script and restart it.

    The flags live in the Bitnami ``start-master.sh`` ConfigMap (the chart's
    ``redis-queue.master.extraFlags``), so they persist across broker restarts
    exactly as the Incident Arena task committed them; ``frappe-admin`` (driven
    by the toolbox's ``reconfigure-infra.sh``) repairs the same script.
    """

    def __init__(
        self,
        fault_flags: list[str],
        component: str,
        healthy_flags: list[str] | None = None,
        expect_min_replicas_to_write: int | None = None,
        expect_dequeue_allowed: bool = False,
        expected_acl_rules: list[str] | None = None,
        expect_appendfsync: tuple[str, ...] | None = None,
    ) -> None:
        super().__init__()
        self.fault_flags = fault_flags
        self.healthy_flags = healthy_flags or HEALTHY_QUEUE_FLAGS
        self.component = component
        self.expect_min_replicas_to_write = expect_min_replicas_to_write
        self.expect_dequeue_allowed = expect_dequeue_allowed
        self.expected_acl_rules = expected_acl_rules
        self.expect_appendfsync = expect_appendfsync

    def deploy_values(self):
        return {"erpnext": {"redis-queue": {"master": {"extraFlags": list(self.healthy_flags)}}}}

    # -- start script ----------------------------------------------------------
    def _set_flags(self, flags: list[str]) -> None:
        app = self.frappe
        current = json.loads(
            app.kubectl.exec_command_checked(
                f"kubectl get configmap {app.REDIS_QUEUE_SCRIPTS} -n {app.namespace} -o json", timeout=60
            )
        )
        script = render_start_script(current["data"]["start-master.sh"], flags)
        patch = json.dumps({"data": {"start-master.sh": script}})
        app.kubectl.exec_command_checked(
            f"kubectl patch configmap {app.REDIS_QUEUE_SCRIPTS} -n {app.namespace} --type merge -p {shlex.quote(patch)}",
            timeout=60,
        )
        self.restart_broker()

    def restart_broker(self) -> None:
        app = self.frappe
        # The script is mounted as a volume; give the kubelet a moment to project it.
        time.sleep(5)
        app.delete_pod_and_wait("statefulset", app.REDIS_QUEUE_STATEFULSET, app.REDIS_QUEUE_POD)

    def inject(self) -> None:
        self._set_flags(self.fault_flags)

    def recover(self) -> None:
        self._set_flags(self.healthy_flags)

    # -- grading -----------------------------------------------------------------
    def _state_checks(self, phase: str, reason: str = "fault_still_present") -> list[CheckResult]:
        app = self.frappe
        results = []
        try:
            maxmemory = int(redis_config(app, "maxmemory") or -1)
        except ValueError:
            maxmemory = -1
        low, high = MAXMEMORY_BAND
        results.append(
            CheckResult(
                f"queue_maxmemory_bounded@{phase}",
                low <= maxmemory <= high,
                reason=reason,
                detail={"maxmemory": maxmemory, "band": [low, high]},
            )
        )
        policy = redis_config(app, "maxmemory-policy")
        results.append(
            CheckResult(
                f"queue_loss_intolerant@{phase}",
                policy == "noeviction",
                reason="unsafe_repair" if reason == "fault_still_present" else reason,
                detail={"maxmemory-policy": policy},
            )
        )
        appendonly = redis_config(app, "appendonly")
        results.append(
            CheckResult(
                f"queue_persistent@{phase}",
                appendonly == "yes",
                reason="unsafe_repair" if reason == "fault_still_present" else reason,
                detail={"appendonly": appendonly},
            )
        )
        if self.expect_appendfsync:
            fsync = redis_config(app, "appendfsync")
            results.append(
                CheckResult(
                    f"queue_fsync_policy@{phase}",
                    fsync in self.expect_appendfsync,
                    reason="unsafe_repair" if reason == "fault_still_present" else reason,
                    detail={"appendfsync": fsync, "allowed": list(self.expect_appendfsync)},
                )
            )
        if self.expect_min_replicas_to_write is not None:
            value = redis_config(app, "min-replicas-to-write")
            results.append(
                CheckResult(
                    f"queue_min_replicas_to_write@{phase}",
                    value == str(self.expect_min_replicas_to_write),
                    reason=reason,
                    detail={"min-replicas-to-write": value},
                )
            )
        if self.expect_dequeue_allowed or self.expected_acl_rules is not None:
            rules = redis_acl_rules(app)
            if self.expected_acl_rules is not None:
                passed = sorted(rules) == sorted(self.expected_acl_rules)
            else:
                passed = "+@all" in rules and not any(f"-{c}" in rules for c in DEQUEUE_COMMANDS)
            results.append(
                CheckResult(f"queue_dequeue_permitted@{phase}", passed, reason=reason, detail={"rules": rules})
            )
        return results

    def checks(self, phase: str) -> list[CheckResult]:
        return self._state_checks(phase)

    def challenges(self):
        def broker_restart() -> CheckResult:
            try:
                self.restart_broker()
            except Exception as exc:
                return CheckResult(
                    "queue_restart_challenge", False, reason="challenge_failed", detail={"error": str(exc)}
                )
            post = self._state_checks("post_restart", reason="repair_not_durable")
            failed = [r.name for r in post if not r.passed]
            return CheckResult(
                "queue_repair_survives_restart",
                not failed,
                reason="repair_not_durable",
                detail={"failed": failed, "checks": [r.as_dict() for r in post]},
            )

        return [broker_restart]

    def describe(self) -> str:
        return (
            "redis-queue broker started with extra flags "
            + " ".join(self.fault_flags)
            + " (persisted in the Bitnami start-master.sh ConfigMap, so they survive a restart)."
        )


# ---------------------------------------------------------------------- guards
class FrappeScopeGuard(FrappeLeg):
    """Incident Arena's stable probes plus 'no worker scaling'.

    Captured before injection; any protected MariaDB global or privilege row
    not owned by one of the problem's legs, the DocType table, and the replica
    counts of the Frappe workloads must be unchanged after the repair.
    """

    WORKLOAD_SELECTOR = "app.kubernetes.io/instance=frappe"

    def __init__(self, owned_globals: tuple[str, ...] = (), grant_legs: tuple[SiteGrantRevocation, ...] = ()) -> None:
        super().__init__()
        self.owned_globals = set(owned_globals)
        self.grant_legs = grant_legs
        self.baseline: dict = {}

    def _snapshot(self) -> dict:
        app = self.frappe
        database = site_database(app)
        owned_grantees = {leg.owned_grantee() for leg in self.grant_legs if leg.owned_grantee()}
        grants = [
            row
            for row in grant_fingerprint(app)
            # The site account's schema privileges are graded by the grant leg.
            if not (row.startswith("schema|") and row.split("|")[1] in owned_grantees and row.split("|")[2] == database)
        ]
        protected = [g for g in PROTECTED_GLOBALS if g not in self.owned_globals]
        deployments = app.kubectl.list_deployments(app.namespace)
        return {
            "globals": global_variables(app, protected),
            "grants": grants,
            "doctype_count": app.mysql(f"SELECT COUNT(*) FROM `{database}`.`tabDocType`").strip(),
            "replicas": {d.metadata.name: d.spec.replicas for d in deployments.items},
            "cache_run_id": _info_field(app.redis("INFO", "server", cache=True), "run_id"),
        }

    def capture_baseline(self) -> None:
        self.baseline = self._snapshot()

    def checks(self, phase: str) -> list[CheckResult]:
        if not self.baseline:
            return []
        now = self._snapshot()
        before = self.baseline
        changed_globals = {
            k: (before["globals"].get(k), v) for k, v in now["globals"].items() if before["globals"].get(k) != v
        }
        grant_delta = sorted(set(now["grants"]) ^ set(before["grants"]))
        # Only workloads that existed before injection (fault helpers come and go).
        replica_delta = {
            k: (v, now["replicas"].get(k)) for k, v in before["replicas"].items() if now["replicas"].get(k) != v
        }
        return [
            CheckResult(
                f"protected_globals_unchanged@{phase}",
                not changed_globals,
                reason="unsafe_repair",
                detail=changed_globals,
            ),
            CheckResult(
                f"unrelated_grants_unchanged@{phase}",
                not grant_delta,
                reason="unsafe_repair",
                detail={"delta": grant_delta},
            ),
            CheckResult(
                f"doctypes_intact@{phase}",
                now["doctype_count"] == before["doctype_count"],
                reason="unsafe_repair",
                detail={"before": before["doctype_count"], "after": now["doctype_count"]},
            ),
            CheckResult(f"no_worker_scaling@{phase}", not replica_delta, reason="unsafe_repair", detail=replica_delta),
            CheckResult(
                f"cache_tier_untouched@{phase}",
                now["cache_run_id"] == before["cache_run_id"],
                reason="unsafe_repair",
                detail={"before": before["cache_run_id"], "after": now["cache_run_id"]},
            ),
        ]


def _info_field(info: str, field: str) -> str:
    match = re.search(rf"^{re.escape(field)}:(\S+)", info, re.MULTILINE)
    return match.group(1) if match else ""


# ---------------------------------------------------------------------- problems
class FrappeProblem(IncidentArenaProblem):
    GROUND_RULES = (
        "Ground rules from the incident runbook: repair the live system in place through its operational "
        "surfaces (database administration, broker configuration, the toolbox helpers). Preserve queued work. "
        "Broad grants, flushing data, cache-style eviction, scaling workers, suppressing traffic, and "
        "restart-only workarounds are not acceptable."
    )
    # The queue legs fill or stall over minutes of traffic.
    PROPAGATION_S = 180

    def create_app(self):
        return Frappe()

    def build_guards(self):
        grant_legs = tuple(leg for leg in self.legs if isinstance(leg, SiteGrantRevocation))
        owned = tuple(leg.variable for leg in self.legs if isinstance(leg, MariaDBGlobal))
        return [FrappeScopeGuard(owned_globals=owned, grant_legs=grant_legs)]


def _acl_denial(*extra: str) -> list[str]:
    return ["--user", "default", "on", "nopass", "~*", "&*", "+@all", *extra]


class FrappeDeskAndQueueOutage(FrappeProblem):
    """Incident Arena 002: per-account connection cap + min-replicas-to-write on a standalone broker."""

    TASK = "002--frappe--07-desk-and-queue-outage-64526986"
    PROBLEM_ID = "frappe_desk_and_queue_outage"

    def build_legs(self):
        return [
            MariaDBGlobal(
                "max_user_connections",
                "8",
                unlimited_or_at_least(16),
                component="mariadb/mariadb.max-user-connections",
                startup_flags="--max-user-connections=500",
            ),
            RedisQueueFlags(
                ["--min-replicas-to-write", "1", *HEALTHY_QUEUE_FLAGS],
                component="redis-queue/redis-queue.config",
                expect_min_replicas_to_write=0,
            ),
        ]


# ---------------------------------------------------------------------- task 005 backlog
MAIL_SINK_MANIFEST = Path(__file__).with_name("manifests") / "frappe-mail-sink.yaml"

# Runs inside the gunicorn pod with the site connected (see Frappe.site_python).
_SEED_BACKLOG = """
import json, zlib
from frappe.utils.background_jobs import get_queue
reports = []
for _ in range(2):
    doc = frappe.get_doc({"doctype": "Prepared Report", "report_name": REPORT, "filters": "{}"})
    doc.insert(ignore_permissions=True)
    reports.append(doc.name)
if not frappe.db.exists("Email Account", "Outbound relay"):
    frappe.get_doc({
        "doctype": "Email Account", "email_account_name": "Outbound relay",
        "email_id": "notifications@example.com", "enable_outgoing": 1, "default_outgoing": 1,
        "smtp_server": "mail-sink", "smtp_port": 1025, "use_tls": 0, "use_ssl_for_outgoing": 0,
        "no_smtp_authentication": 1,
    }).insert(ignore_permissions=True)
mails, subjects = [], []
for i in range(4):
    subject = "Quarterly access review reminder %d (%s)" % (i + 1, frappe.generate_hash(length=8))
    queued = frappe.sendmail(
        recipients=["reviewer%d@example.com" % (i + 1)], sender="notifications@example.com",
        subject=subject, message="Please complete your quarterly access review.", now=False,
    )
    frappe.enqueue(
        "frappe.email.doctype.email_queue.email_queue.send_now",
        queue="long", name=queued.name, enqueue_after_commit=True,
    )
    mails.append(queued.name)
    subjects.append(subject)
frappe.db.commit()
queue = get_queue("long")
conn = queue.connection
stranded = None
for job_id in conn.lrange(queue.key, 0, -1):
    data = conn.hget(b"rq:job:" + job_id, "data")
    try:
        payload = zlib.decompress(data or b"")
    except zlib.error:
        continue
    if b"generate_report" in payload and reports[0].encode() in payload:
        conn.lrem(queue.key, 0, job_id)
        stranded = job_id.decode()
        break
print(json.dumps({"reports": reports, "mails": mails, "subjects": subjects, "stranded_job": stranded}))
"""


class StrandedBacklogSeed(FrappeLeg):
    """Background work accepted before the outage: two Prepared Reports and four emails.

    Mirrors Incident Arena's ``stranded_prepared_report_with_emails`` seed. It
    runs while the queue user cannot dequeue, so every job waits in Redis; one
    report's job id is then dropped from the ``long`` queue list (its job hash
    stays ``queued``), so repairing the broker alone never completes it. Each
    email must reach the in-cluster mailbox exactly once.
    """

    component = "redis-queue/redis-queue.acl"
    REPORT = "Database Storage Usage By Tables"

    def __init__(self) -> None:
        super().__init__()
        self.seed: dict = {}

    def inject(self) -> None:
        app = self.frappe
        app.kubectl.apply_configs(app.namespace, str(MAIL_SINK_MANIFEST))
        app.wait_rollout("deployment", "mail-sink", 300)
        out = app.site_python(f"REPORT = {self.REPORT!r}\n" + _SEED_BACKLOG)
        self.seed = json.loads(out.strip().splitlines()[-1])
        if not self.seed.get("stranded_job"):
            raise RuntimeError(f"could not strand a Prepared Report job: {self.seed}")

    def recover(self) -> None:
        """Re-deliver the stranded report job (the reference repair's reconciliation step).

        Runs after the queue leg is repaired; the mail sink stays up so the
        queued emails can still be delivered (namespace teardown removes it).
        """
        job_id = self.seed.get("stranded_job")
        if not job_id:
            return
        self.frappe.site_python(
            "from frappe.utils.background_jobs import get_queue\n"
            "queue = get_queue('long')\n"
            "conn = queue.connection\n"
            f"job = {job_id!r}.encode()\n"
            "if job not in conn.lrange(queue.key, 0, -1) and conn.hget(b'rq:job:' + job, 'status') == b'queued':\n"
            "    conn.lpush(queue.key, job)\n"
            "print('redelivered')\n"
        )

    def _report_states(self) -> dict[str, str]:
        names = ",".join(f"'{n}'" for n in self.seed.get("reports", []))
        if not names:
            return {}
        database = site_database(self.frappe)
        rows = self.frappe.mysql(f"SELECT name, status FROM `{database}`.`tabPrepared Report` WHERE name IN ({names})")
        return dict(line.split("\t", 1) for line in rows.splitlines() if "\t" in line)

    def _delivered_subjects(self) -> list[str]:
        status, body = self.frappe.http("http://mail-sink:8025/api/v1/messages?limit=500")
        if status != 200:
            raise RuntimeError(f"mail sink returned {status}")
        return [m.get("Subject", "") for m in json.loads(body).get("messages", [])]

    def checks(self, phase: str) -> list[CheckResult]:
        if phase != "soak_end" or not self.seed:
            return []
        states = self._report_states()
        delivered = self._delivered_subjects()
        counts = {s: delivered.count(s) for s in self.seed.get("subjects", [])}
        return [
            CheckResult(
                "accepted_reports_completed",
                len(states) == 2 and all(v == "Completed" for v in states.values()),
                detail={"reports": states, "stranded_job": self.seed.get("stranded_job")},
            ),
            CheckResult(
                "accepted_email_delivered_once",
                bool(counts) and all(c == 1 for c in counts.values()),
                detail={"deliveries": counts},
            ),
        ]

    def describe(self) -> str:
        return (
            "Two Prepared Reports and four emails were accepted while the queue was unusable; one report's RQ job "
            "was dropped from the long queue list (job hash still queued), so it must be re-delivered by hand."
        )


class FrappeWritesAndQueueOOM(FrappeProblem):
    """Incident Arena 005: read-only MariaDB masking revoked grants + 4mb queue that cannot dequeue."""

    TASK = "005--frappe--07-writes-and-queue-oom-f1db8f42"
    PROBLEM_ID = "frappe_writes_and_queue_oom"

    QUEUE_BASE = [
        "--appendonly",
        "yes",
        "--appendfsync",
        "everysec",
        "--user",
        "default",
        "on",
        "nopass",
        "~*",
        "&*",
        "+@all",
        "-flushall",
        "-flushdb",
    ]

    def deploy_values(self):
        values = self.task.task_values
        return {
            "images": values["images"],
            # A deliberately tiny, evicting cache tier: a decoy, not a cause.
            "erpnext": {
                "redis-cache": {"master": {"extraFlags": ["--maxmemory", "4mb", "--maxmemory-policy", "allkeys-lru"]}}
            },
        }

    def build_legs(self):
        healthy = ["--maxmemory", "64mb", "--maxmemory-policy", "noeviction", *self.QUEUE_BASE]
        faulty = [
            "--maxmemory",
            "4mb",
            "--maxmemory-policy",
            "noeviction",
            *self.QUEUE_BASE,
            "-blpop",
            "-blmove",
            "-brpop",
        ]
        return [
            RedisQueueFlags(
                faulty,
                component="redis-queue/redis-queue.config",
                healthy_flags=healthy,
                expected_acl_rules=["+@all", "-flushall", "-flushdb"],
                expect_appendfsync=("everysec", "always"),
            ),
            StrandedBacklogSeed(),
            MariaDBGlobal("read_only", "1", is_off, component="mariadb/mariadb.read-only"),
            SiteGrantRevocation("INSERT", "UPDATE"),
        ]

    def recovery_order(self):
        # Unlock MariaDB, repair the broker, then reconcile the stranded backlog.
        queue, seed, read_only, grants = self.legs
        return [grants, read_only, queue, seed]


__all__ = [
    "FrappeDeskAndQueueOutage",
    "FrappeWritesAndQueueOOM",
]
