"""Slack-spine incidents ported from Incident Arena (tasks 007-019).

The Slack clone's faults are operational: undersized per-role connection pools,
runtime "consistency" toggles pushed onto a role's /admin/event registry, a
database maintenance schedule that collides with the write peaks, a non-atomic
sequencer mode, and two image builds that leak a ``channel_seq`` row lock.
Compound incidents combine them, so fixing the loudest cause is not enough.
"""

from __future__ import annotations

import json
import re
import shlex
import time

from sregym.conductor.oracles.incident_arena import CheckResult
from sregym.conductor.problems.incident_arena.base import FaultLeg, IncidentArenaProblem
from sregym.service.apps.incident_arena import SlackSpine
from sregym.service.apps.incident_arena.slack_spine import APP_ROLES

HEALTHY_POOL = {"pool_size": 20, "max_overflow": 10}
# Incident Arena "resource_ceiling" pack: no role may exceed the peer pool budget.
POOL_CEILING = {"pool_size": 20, "max_overflow": 10}
POOL_TIMEOUT_FLOOR_S = 1
# Healthy per-role handler hold (chart default); repairs may not raise it.
HEALTHY_HOLD_MS = {
    "auth": 5,
    "workspace": 8,
    "channel": 10,
    "message": 150,
    "thread": 12,
    "file": 8,
    "search": 8,
    "notification": 8,
    "platform": 8,
}

# A backend that has sat idle inside a transaction while holding channel_seq.
# Healthy sends pass through "idle in transaction" for milliseconds between
# statements, so only sessions idle for longer than LEAK_AGE_S count; it also
# exceeds the 10s idle_in_transaction_session_timeout of the reference repair.
LEAK_AGE_S = 15
_HOLDERS = (
    "SELECT DISTINCT a.pid FROM pg_stat_activity a "
    "JOIN pg_locks l ON l.pid = a.pid JOIN pg_class c ON c.oid = l.relation "
    "WHERE a.state = 'idle in transaction' AND c.relname = 'channel_seq' AND a.pid <> pg_backend_pid() "
    "AND now() - a.state_change > interval '{age} seconds'"
)
LOCK_HOLDER_SQL = f"SELECT count(*) FROM ({_HOLDERS.format(age=LEAK_AGE_S)}) h"
TERMINATE_HOLDERS_SQL = f"SELECT count(pg_terminate_backend(pid)) FROM ({_HOLDERS.format(age=1)}) h"


def patch_role_db_config(text: str, role: str, updates: dict[str, int]) -> str:
    """Rewrite ``roles.<role>.db.<key>`` values in the rendered app.yaml, keeping comments."""
    lines = text.splitlines()
    out = []
    in_role = in_db = False
    seen = set()
    for line in lines:
        if re.match(r"^  \S", line):
            in_role = line.strip() == f"{role}:"
            in_db = False
        elif in_role and re.match(r"^    \S", line):
            in_db = line.strip() == "db:"
        elif in_role and in_db:
            match = re.match(r"^(      )([a-z_]+):(\s*)(\S.*)$", line)
            if match and match.group(2) in updates:
                key = match.group(2)
                line = f"{match.group(1)}{key}: {updates[key]}"
                seen.add(key)
        out.append(line)
    missing = set(updates) - seen
    if missing:
        raise RuntimeError(f"app.yaml has no roles.{role}.db keys {sorted(missing)}")
    return "\n".join(out) + ("\n" if text.endswith("\n") else "")


def flatten(payload, prefix: str = "") -> dict[str, object]:
    """``{"db": {"pool_size": 20}}`` -> ``{"db.pool_size": 20}``."""
    flat = {}
    for key, value in (payload or {}).items():
        path = f"{prefix}{key}"
        if isinstance(value, dict):
            flat.update(flatten(value, f"{path}."))
        else:
            flat[path] = value
    return flat


def in_safe_maintenance_window(offset_s: float, period_s: float = 60, duration_s: float = 8) -> bool:
    """Incident Arena's safe offsets for the 60s cycle (30s warmup, 20s write peak).

    A complete run must finish before the peak starts (offset <= 22) or start
    after it ends (50 <= offset < 60).
    """
    return (0 <= offset_s <= 22) or (50 <= offset_s < period_s)


class SlackLeg(FaultLeg):
    @property
    def slack(self) -> SlackSpine:
        return self.app

    def deployment_env(self, deployment: str) -> dict[str, str]:
        raw = self.slack.kubectl.exec_command_checked(
            f"kubectl get deployment {deployment} -n {self.namespace} -o json", timeout=60
        )
        containers = json.loads(raw)["spec"]["template"]["spec"]["containers"]
        return {e["name"]: e.get("value") for c in containers for e in c.get("env", []) if "name" in e}

    def holder_count(self) -> int:
        return int(self.slack.psql(LOCK_HOLDER_SQL).strip() or 0)

    def wait_for(self, predicate, timeout_s: float, interval_s: float = 5, what: str = "condition") -> None:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            try:
                if predicate():
                    return
            except Exception:
                pass
            time.sleep(interval_s)
        raise RuntimeError(f"timed out after {timeout_s}s waiting for {what}")


# ---------------------------------------------------------------------- pools
class RolePoolSize(SlackLeg):
    """An undersized ``roles.<role>.db`` connection pool (pool_size + max_overflow).

    Written to the app.yaml ConfigMap (so it survives restarts, as the task
    committed it) and applied live through ``PUT /admin/config`` +
    ``POST /admin/reload``.
    """

    def __init__(self, role: str, pool_size: int, max_overflow: int, floors: tuple[int, int] | None = (10, 5)) -> None:
        super().__init__()
        self.role = role
        self.fault = {"pool_size": pool_size, "max_overflow": max_overflow}
        self.floors = floors
        self.component = f"{role}/{role}.db-pool"

    def owned_keys(self) -> set[tuple[str, str]]:
        return {(self.role, "db.pool_size"), (self.role, "db.max_overflow")}

    def _apply(self, pool: dict[str, int]) -> None:
        app = self.slack
        current = json.loads(
            app.kubectl.exec_command_checked(
                f"kubectl get configmap {app.APP_CONFIG_MAP} -n {app.namespace} -o json", timeout=60
            )
        )
        text = patch_role_db_config(current["data"]["app.yaml"], self.role, pool)
        patch = json.dumps({"data": {"app.yaml": text}})
        app.kubectl.exec_command_checked(
            f"kubectl patch configmap {app.APP_CONFIG_MAP} -n {app.namespace} --type merge -p {shlex.quote(patch)}",
            timeout=60,
        )
        app.admin(self.role, "/admin/config", "PUT", {"db": pool})
        app.admin(self.role, "/admin/reload", "POST")

    def inject(self) -> None:
        self._apply(self.fault)

    def recover(self) -> None:
        self._apply(HEALTHY_POOL)

    def checks(self, phase: str) -> list[CheckResult]:
        db = self.slack.admin(self.role, "/admin/config").get("db", {})
        pool, overflow = int(db.get("pool_size", 0)), int(db.get("max_overflow", 0))
        if self.floors:
            enough = pool >= self.floors[0] and overflow >= self.floors[1]
        else:
            enough = pool + overflow > self.fault["pool_size"] + self.fault["max_overflow"]
        detail = {"role": self.role, "db": db}
        return [
            CheckResult(f"{self.role}_pool_capacity_restored@{phase}", enough, detail=detail),
            CheckResult(
                f"{self.role}_pool_within_ceiling@{phase}",
                pool <= POOL_CEILING["pool_size"] and overflow <= POOL_CEILING["max_overflow"],
                reason="unsafe_repair",
                detail=detail,
            ),
            CheckResult(
                f"{self.role}_pool_timeout_floor@{phase}",
                float(db.get("pool_timeout_s", 0)) >= POOL_TIMEOUT_FLOOR_S,
                reason="unsafe_repair",
                detail=detail,
            ),
            CheckResult(
                f"{self.role}_hold_ceiling@{phase}",
                float(db.get("hold_ms", 0)) <= HEALTHY_HOLD_MS[self.role],
                reason="unsafe_repair",
                detail=detail,
            ),
        ]

    def describe(self) -> str:
        return (
            f"roles.{self.role}.db pool_size={self.fault['pool_size']} max_overflow={self.fault['max_overflow']} "
            f"in the app-config ConfigMap and live on svc-{self.role} (every peer role runs 20/10)."
        )


# ---------------------------------------------------------------------- runtime events
class AdminEvent(SlackLeg):
    """A runtime toggle pushed onto roles' ``/admin/event`` registry mid-episode.

    ``store_consistency_strict`` makes every Redis operation of the role pay
    ``STORE_HOLD_MS``; ``read_consistency_strict`` sends every channel authz
    resolve to PostgreSQL holding a pooled connection for ``ACL_HOLD_MS``. The
    hold doses are deploy-time env (latent until the event fires), exactly as
    the Incident Arena task committed them.
    """

    def __init__(
        self,
        event: str,
        roles: list[str],
        component: str,
        hold_env: dict[str, str] | None = None,
        keep_active: bool = False,
    ) -> None:
        super().__init__()
        self.event = event
        self.roles = roles
        self.component = component
        self.hold_env = hold_env or {}
        self.keep_active = keep_active
        # A mandated window that must stay on is the incident's context, not its cause.
        self.is_cause = not keep_active
        self.restart_allowed: set[str] = set()
        self.identities: dict[str, dict] = {}

    def deploy_values(self):
        if not self.hold_env:
            return {}
        return {"app": {"roles": {role: {"env": dict(self.hold_env)} for role in self.roles}}}

    def _set(self, active: bool) -> None:
        for role in self.roles:
            self.slack.admin(role, "/admin/event", "PUT", {"name": self.event, "active": active})

    def _active(self, role: str) -> bool:
        return self.event in self.slack.admin(role, "/admin/event").get("active", [])

    def capture_baseline(self) -> None:
        self.identities = {role: self.slack.pod_identities(SlackSpine.role_selector(role)) for role in self.roles}

    def inject(self) -> None:
        self._set(True)
        inactive = [role for role in self.roles if not self._active(role)]
        if inactive:
            raise RuntimeError(f"{self.event} did not activate on {inactive}")

    def recover(self) -> None:
        # A mandated window stays on; the incident is repaired around it.
        if not self.keep_active:
            self._set(False)

    def checks(self, phase: str) -> list[CheckResult]:
        results = []
        for role in self.roles:
            active = self._active(role)
            if self.keep_active:
                results.append(
                    CheckResult(
                        f"{role}_{self.event}_kept_active@{phase}",
                        active,
                        reason="unsafe_repair",
                        detail={"role": role, "active": active, "policy": "must stay on"},
                    )
                )
            else:
                results.append(
                    CheckResult(
                        f"{role}_{self.event}_cleared@{phase}", not active, detail={"role": role, "active": active}
                    )
                )
            if role not in self.restart_allowed and self.identities.get(role):
                now = self.slack.pod_identities(SlackSpine.role_selector(role))
                results.append(
                    CheckResult(
                        f"{role}_not_restarted@{phase}",
                        now == self.identities[role],
                        reason="restart_masked_fault",
                        detail={"before": self.identities[role], "after": now},
                    )
                )
        return results

    def challenges(self):
        if not self.keep_active:
            return []

        def reassert() -> CheckResult:
            # Incident Arena re-fires the mandated window during the soak.
            try:
                self._set(True)
                ok = all(self._active(role) for role in self.roles)
            except Exception as exc:
                return CheckResult(
                    f"{self.event}_window_reasserted", False, reason="challenge_failed", detail={"error": str(exc)}
                )
            return CheckResult(f"{self.event}_window_reasserted", ok, reason="challenge_failed")

        return [reassert]

    def describe(self) -> str:
        doses = ", ".join(f"{k}={v}" for k, v in self.hold_env.items())
        return (
            f'PUT /admin/event {{"name": "{self.event}", "active": true}} on '
            + ", ".join(f"svc-{r}" for r in self.roles)
            + (f" (hold dose {doses})." if doses else ".")
        )


# ---------------------------------------------------------------------- maintenance
class MaintenanceSchedule(SlackLeg):
    """The durable PostgreSQL checkpoint schedule moved into the write peaks.

    The controller (db pod sidecar, ``db-maintenance:8081``) keeps its schedule
    in PostgreSQL and runs real dirty-write + CHECKPOINT cycles every 60s at
    ``offset_s`` from the load generator's epoch. Offset 35 lands inside each
    20-second write peak; the healthy schedule runs at 55.
    """

    component = "db/db.maintenance-controller"
    FAULT_OFFSET = 35
    HEALTHY = {"enabled": True, "period_s": 60, "offset_s": 55, "duration_s": 8}

    def __init__(self) -> None:
        super().__init__()
        self.completed_at: dict[str, int] = {}

    def deploy_values(self):
        return {
            "components": {"maintenanceController": {"enabled": True}},
            "maintenanceController": {
                "enabled": True,
                "periodS": self.HEALTHY["period_s"],
                "offsetS": self.HEALTHY["offset_s"],
                "durationS": self.HEALTHY["duration_s"],
                "readinessProbeTimeoutSeconds": 15,
            },
            "resources": {
                "maintenanceController": {
                    "requests": {"cpu": "250m", "memory": "96Mi"},
                    "limits": {"cpu": "1000m", "memory": "192Mi"},
                }
            },
        }

    def _state(self) -> dict:
        status, body = self.slack.http(SlackSpine.MAINTENANCE_URL)
        if status != 200:
            raise RuntimeError(f"maintenance API returned {status}: {body[:200]}")
        return json.loads(body)

    def _put(self, schedule: dict) -> None:
        status, body = self.slack.http(SlackSpine.MAINTENANCE_URL, method="PUT", body=schedule)
        if status != 200:
            raise RuntimeError(f"maintenance PUT returned {status}: {body[:200]}")

    def capture_baseline(self) -> None:
        self.completed_at["baseline"] = int(self._state().get("counters", {}).get("failed", 0))

    def inject(self) -> None:
        self._put({**self.HEALTHY, "offset_s": self.FAULT_OFFSET})

    def recover(self) -> None:
        self._put(self.HEALTHY)

    def checks(self, phase: str) -> list[CheckResult]:
        state = self._state()
        schedule = state.get("schedule", {})
        counters = state.get("counters", {})
        offset = float(schedule.get("offset_s", -1))
        results = [
            CheckResult(
                f"maintenance_offset_clear_of_peaks@{phase}", in_safe_maintenance_window(offset), detail=schedule
            ),
            CheckResult(
                f"maintenance_still_scheduled@{phase}",
                bool(schedule.get("enabled"))
                and float(schedule.get("period_s", 0)) == self.HEALTHY["period_s"]
                and float(schedule.get("duration_s", 0)) == self.HEALTHY["duration_s"],
                reason="unsafe_repair",
                detail=schedule,
            ),
            CheckResult(
                f"maintenance_runs_healthy@{phase}",
                int(counters.get("failed", 0)) <= self.completed_at.get("baseline", 0),
                reason="unsafe_repair",
                detail=counters,
            ),
        ]
        completed = int(counters.get("completed", 0))
        if phase == "declaration":
            self.completed_at["declaration"] = completed
        elif "declaration" in self.completed_at:
            results.append(
                CheckResult(
                    "maintenance_checkpoints_advanced",
                    completed > self.completed_at["declaration"],
                    reason="unsafe_repair",
                    detail={"at_declaration": self.completed_at["declaration"], "at_soak_end": completed},
                )
            )
        return results

    def describe(self) -> str:
        return (
            f"PUT {SlackSpine.MAINTENANCE_URL} offset_s={self.FAULT_OFFSET} (period 60s, duration 8s): durable "
            "checkpoints now start inside every 20-second write peak (healthy offset 55)."
        )


# ---------------------------------------------------------------------- sequencer
class SequencerMode(SlackLeg):
    """``SEQUENCER_MODE=rmw`` on svc-message: a non-atomic read-modify-write allocator.

    Concurrent same-channel sends read the same cursor and persist the same
    sequence number, silently corrupting history order (no 5xx). A repair must
    select the atomic allocator durably and losslessly re-sequence the rows.
    """

    component = "message/message.sequencer"

    def __init__(self) -> None:
        super().__init__()
        self.message_count = 0

    def _count(self) -> int:
        return int(self.slack.psql("SELECT count(*) FROM messages").strip() or 0)

    def capture_baseline(self) -> None:
        self.message_count = self._count()

    def inject(self) -> None:
        self.slack.kubectl.exec_command_checked(
            f"kubectl set env deployment/svc-message -n {self.namespace} -c app SEQUENCER_MODE=rmw", timeout=60
        )
        self.slack.wait_rollout("deployment", "svc-message")

    def recover(self) -> None:
        self.slack.kubectl.exec_command_checked(
            f"kubectl set env deployment/svc-message -n {self.namespace} -c app SEQUENCER_MODE-", timeout=60
        )
        self.slack.wait_rollout("deployment", "svc-message")
        self.slack.psql(
            "BEGIN; SELECT 1 FROM channel_seq FOR UPDATE; "
            "WITH renum AS (SELECT id, ROW_NUMBER() OVER (PARTITION BY channel_id ORDER BY id) AS new_seq FROM messages) "
            "UPDATE messages m SET seq = r.new_seq FROM renum r WHERE m.id = r.id AND m.seq <> r.new_seq; "
            "UPDATE channel_seq cs SET last_seq = mx.max_seq FROM (SELECT channel_id, max(seq) AS max_seq "
            "FROM messages GROUP BY channel_id) mx WHERE cs.channel_id = mx.channel_id AND cs.last_seq <> mx.max_seq; "
            "COMMIT;"
        )

    def _mode_checks(self, phase: str, reason: str = "fault_still_present") -> list[CheckResult]:
        env_mode = self.deployment_env("svc-message").get("SEQUENCER_MODE")
        live_mode = self.slack.admin("message", "/admin/sequencer").get("mode")
        return [
            CheckResult(
                f"sequencer_atomic_persisted@{phase}",
                env_mode in (None, "", "atomic"),
                reason=reason,
                detail={"SEQUENCER_MODE": env_mode},
            ),
            CheckResult(
                f"sequencer_atomic_live@{phase}", live_mode == "atomic", reason=reason, detail={"mode": live_mode}
            ),
        ]

    def checks(self, phase: str) -> list[CheckResult]:
        results = self._mode_checks(phase)
        duplicates = int(
            self.slack.psql(
                "SELECT count(*) FROM (SELECT channel_id, seq FROM messages GROUP BY channel_id, seq HAVING count(*) > 1) d"
            ).strip()
            or 0
        )
        sparse = int(
            self.slack.psql(
                "SELECT count(*) FROM (SELECT channel_id FROM messages GROUP BY channel_id "
                "HAVING min(seq) <> 1 OR max(seq) <> count(*)) g"
            ).strip()
            or 0
        )
        cursors = int(
            self.slack.psql(
                "SELECT count(*) FROM channel_seq cs JOIN (SELECT channel_id, max(seq) AS mx FROM messages "
                "GROUP BY channel_id) m USING (channel_id) WHERE cs.last_seq <> m.mx"
            ).strip()
            or 0
        )
        count = self._count()
        results += [
            CheckResult(f"no_duplicate_sequences@{phase}", duplicates == 0, detail={"duplicate_pairs": duplicates}),
            CheckResult(f"sequences_dense@{phase}", sparse == 0, detail={"non_dense_channels": sparse}),
            CheckResult(f"cursors_match_history@{phase}", cursors == 0, detail={"mismatched_cursors": cursors}),
            CheckResult(
                f"accepted_messages_preserved@{phase}",
                count >= self.message_count,
                reason="unsafe_repair",
                detail={"before": self.message_count, "after": count},
            ),
        ]
        return results

    def challenges(self):
        def restart() -> CheckResult:
            try:
                self.slack.rollout_restart("deployment", "svc-message")
            except Exception as exc:
                return CheckResult(
                    "message_restart_challenge", False, reason="challenge_failed", detail={"error": str(exc)}
                )
            post = self._mode_checks("post_restart", reason="repair_not_durable")
            failed = [r.name for r in post if not r.passed]
            return CheckResult(
                "sequencer_survives_restart", not failed, reason="repair_not_durable", detail={"failed": failed}
            )

        return [restart]

    def describe(self) -> str:
        return "SEQUENCER_MODE=rmw on the svc-message Deployment (non-atomic read-modify-write channel_seq allocation)."


# ---------------------------------------------------------------------- image-layer lock leaks
class LeakedSequencerLock(SlackLeg):
    """The deployed release leaks a ``channel_seq`` row lock from an idle transaction.

    Incident Arena ships this build as *the* release (Tier-2 image fault): a
    boot transaction takes ``SELECT ... FOR UPDATE`` on chan-0's cursor and
    never commits, so chan-0 writes block and starve the message pool. A
    restart re-arms it; the release must stay, so the repair is operational.
    """

    component = "message/message.sequencer"
    RECURRENCE_WAIT_S = 45

    def deploy_values(self):
        # The task's pinned release build (shared by every application role).
        return {"images": {"app": self.problem.task.task_values["images"]["app"]}}

    def inject(self) -> None:
        # The release is the fault; wait until its leaked lock is held.
        self.wait_for(lambda: self.holder_count() > 0, timeout_s=600, what="a leaked channel_seq lock holder")

    def recover(self) -> None:
        self.slack.psql(TERMINATE_HOLDERS_SQL)
        self.slack.psql("ALTER SYSTEM SET idle_in_transaction_session_timeout = '10s'")
        self.slack.psql("SELECT pg_reload_conf()")

    def checks(self, phase: str) -> list[CheckResult]:
        holders = self.holder_count()
        return [CheckResult(f"no_channel_seq_lock_holder@{phase}", holders == 0, detail={"holders": holders})]

    def challenges(self):
        def restart() -> CheckResult:
            try:
                self.slack.rollout_restart("deployment", "svc-message")
            except Exception as exc:
                return CheckResult(
                    "message_restart_challenge", False, reason="challenge_failed", detail={"error": str(exc)}
                )
            time.sleep(self.RECURRENCE_WAIT_S)
            holders = self.holder_count()
            return CheckResult(
                "no_lock_holder_after_restart",
                holders == 0,
                reason="repair_not_durable",
                detail={"holders": holders, "waited_s": self.RECURRENCE_WAIT_S},
            )

        return [restart]

    def describe(self) -> str:
        return (
            "The deployed slack-app release opens a boot transaction that runs SELECT ... FOR UPDATE on chan-0's "
            "channel_seq row and stays idle in transaction, so chan-0 sends block and exhaust the message pool."
        )


class SessionHandoffLock(SlackLeg):
    """The deployed release's session-scoped sequencer handoff leaks a cohort's row lock.

    The message role persists its checkpoint/handoff mode in ``app_kv_state``.
    In ``session`` mode a retry-reused database lease keeps one seeded channel
    cohort's ``channel_seq`` row locked in an idle transaction. The durable
    repair persists ``request`` mode (``PUT /admin/checkpoint``) and reloads.
    """

    component = "message/message.sequencer"
    RECURRENCE_WAIT_S = 30

    def deploy_values(self):
        # The task's pinned release build (shared by every application role).
        return {"images": {"app": self.problem.task.task_values["images"]["app"]}}

    def _mode(self) -> dict:
        return self.slack.admin("message", "/admin/checkpoint")

    def inject(self) -> None:
        self.wait_for(lambda: self.holder_count() > 0, timeout_s=600, what="a leaked channel_seq lock holder")

    def recover(self) -> None:
        self.slack.admin("message", "/admin/checkpoint", "PUT", {"mode": "request"})
        self.slack.admin("message", "/admin/reload", "POST")
        self.slack.psql(TERMINATE_HOLDERS_SQL)

    def _state_checks(self, phase: str, reason: str = "fault_still_present") -> list[CheckResult]:
        mode = self._mode()
        holders = self.holder_count()
        return [
            CheckResult(
                f"request_mode_persisted@{phase}",
                mode.get("mode") == "request" and mode.get("persisted", True) is not False,
                reason=reason,
                detail=mode,
            ),
            CheckResult(
                f"no_channel_seq_lock_holder@{phase}", holders == 0, reason=reason, detail={"holders": holders}
            ),
        ]

    def checks(self, phase: str) -> list[CheckResult]:
        return self._state_checks(phase)

    def challenges(self):
        def restart() -> CheckResult:
            try:
                self.slack.rollout_restart("deployment", "svc-message")
            except Exception as exc:
                return CheckResult(
                    "message_restart_challenge", False, reason="challenge_failed", detail={"error": str(exc)}
                )
            time.sleep(self.RECURRENCE_WAIT_S)
            post = self._state_checks("post_restart", reason="repair_not_durable")
            failed = [r.name for r in post if not r.passed]
            return CheckResult(
                "handoff_repair_survives_restart",
                not failed,
                reason="repair_not_durable",
                detail={"checks": [r.as_dict() for r in post]},
            )

        return [restart]

    def describe(self) -> str:
        return (
            "The deployed slack-app release runs the message sequencer handoff in session mode; a retry-reused "
            "lease leaves one channel cohort's channel_seq row locked in an idle transaction."
        )


# ---------------------------------------------------------------------- guard
class SlackScopeGuard(SlackLeg):
    """Incident Arena's repair-scope, resource-ceiling and release checks.

    * every role's live ``/admin/config`` is unchanged except keys a pool leg owns;
    * every application Deployment still runs the release image it started with;
    * PostgreSQL capacity and settings are unchanged (except ``allowed_settings``).
    """

    def __init__(self, owned_keys: set[tuple[str, str]] | None = None, allowed_settings: tuple[str, ...] = ()) -> None:
        super().__init__()
        self.owned_keys = owned_keys or set()
        self.allowed_settings = set(allowed_settings)
        self.baseline: dict = {}

    def _configs(self) -> dict[str, dict]:
        return {role: flatten(self.slack.admin(role, "/admin/config")) for role in APP_ROLES}

    def _images(self) -> dict[str, list[str]]:
        raw = json.loads(
            self.slack.kubectl.exec_command_checked(f"kubectl get deployments -n {self.namespace} -o json", timeout=60)
        )
        return {
            d["metadata"]["name"]: sorted(c["image"] for c in d["spec"]["template"]["spec"]["containers"])
            for d in raw["items"]
            if d["metadata"]["name"].startswith("svc-")
        }

    def _pg_settings(self) -> dict[str, str]:
        rows = self.slack.psql(
            "SELECT 'file:' || name || '=' || coalesce(setting, '') FROM pg_file_settings WHERE applied "
            "UNION ALL SELECT 'scope:' || coalesce(r.rolname, '*') || '@' || coalesce(d.datname, '*') || '=' || "
            "array_to_string(s.setconfig, ',') FROM pg_db_role_setting s LEFT JOIN pg_roles r ON r.oid = s.setrole "
            "LEFT JOIN pg_database d ON d.oid = s.setdatabase "
            "UNION ALL SELECT 'live:max_connections=' || current_setting('max_connections')"
        )
        settings = {}
        for line in rows.splitlines():
            key, _, value = line.partition("=")
            if key:
                settings[key] = value
        return settings

    def _filtered_settings(self, settings: dict[str, str]) -> dict[str, str]:
        out = {}
        for key, value in settings.items():
            if key.startswith("file:") and key[5:] in self.allowed_settings:
                continue
            if key.startswith("scope:"):
                value = ",".join(
                    part for part in value.split(",") if part and part.split("=", 1)[0] not in self.allowed_settings
                )
                if not value:
                    continue
            out[key] = value
        return out

    def capture_baseline(self) -> None:
        self.baseline = {"configs": self._configs(), "images": self._images(), "pg": self._pg_settings()}

    def checks(self, phase: str) -> list[CheckResult]:
        if not self.baseline:
            return []
        configs = self._configs()
        changed = {}
        for role, flat in configs.items():
            before = self.baseline["configs"].get(role, {})
            for key in set(flat) | set(before):
                if (role, key) in self.owned_keys:
                    continue
                if flat.get(key) != before.get(key):
                    changed[f"{role}.{key}"] = [before.get(key), flat.get(key)]
        ceiling = {
            role: {k: flat.get(f"db.{k}") for k in POOL_CEILING}
            for role, flat in configs.items()
            if any(float(flat.get(f"db.{k}", 0) or 0) > v for k, v in POOL_CEILING.items())
        }
        images = self._images()
        image_delta = {
            k: [self.baseline["images"].get(k), v] for k, v in images.items() if self.baseline["images"].get(k) != v
        }
        pg_before = self._filtered_settings(self.baseline["pg"])
        pg_now = self._filtered_settings(self._pg_settings())
        pg_delta = sorted(set(pg_before.items()) ^ set(pg_now.items()))
        return [
            CheckResult(f"repair_scope@{phase}", not changed, reason="unsafe_repair", detail={"changed_keys": changed}),
            CheckResult(f"resource_ceiling@{phase}", not ceiling, reason="unsafe_repair", detail=ceiling),
            CheckResult(
                f"release_image_unchanged@{phase}", not image_delta, reason="unsafe_repair", detail=image_delta
            ),
            CheckResult(
                f"postgres_scope_unchanged@{phase}", not pg_delta, reason="unsafe_repair", detail={"delta": pg_delta}
            ),
        ]


# ---------------------------------------------------------------------- problems
class SlackSpineProblem(IncidentArenaProblem):
    GROUND_RULES = (
        "Ground rules from the incident runbook: repair the live system in place through its operational surfaces "
        "(each service's /admin API, the maintenance API, database administration, service configuration). The "
        "deployed application release is approved and must stay as it is. Restarting or rescaling workloads to mask "
        "a problem, raising capacity limits beyond the peer roles' budget, disabling safeguards or routine "
        "maintenance, and suppressing traffic are not acceptable."
    )
    #: PostgreSQL settings a repair may legitimately add (the task's allowed scope).
    ALLOWED_PG_SETTINGS: tuple[str, ...] = ()

    def create_app(self):
        return SlackSpine()

    def build_guards(self):
        owned = set()
        for leg in self.legs:
            if isinstance(leg, RolePoolSize):
                owned |= leg.owned_keys()
        restart_ok = {leg.role for leg in self.legs if isinstance(leg, RolePoolSize)}
        for leg in self.legs:
            if isinstance(leg, AdminEvent):
                leg.restart_allowed = restart_ok
        return [SlackScopeGuard(owned_keys=owned, allowed_settings=self.ALLOWED_PG_SETTINGS)]

    def deploy_values(self):
        # Per-task load generator sizing (the session-heavy profiles need more memory).
        resources = self.task.task_values.get("resources") or {}
        return {"resources": {"loadgen": resources["loadgen"]}} if "loadgen" in resources else {}


class SlackSplitSequencer(SlackSpineProblem):
    """Incident Arena 007: non-atomic sequencer silently duplicates per-channel sequence numbers."""

    TASK = "007--slack-spine--06-F3-split-sequencer-eef5b438"
    PROBLEM_ID = "incident_arena_slack_split_sequencer"

    def build_legs(self):
        return [SequencerMode()]


class SlackMaintenanceCollision(SlackSpineProblem):
    """Incident Arena 008: maintenance checkpoints scheduled inside the write peaks."""

    TASK = "008--slack-spine--06-F4-maintenance-collision-cc222870"
    PROBLEM_ID = "incident_arena_slack_maintenance_collision"

    def build_legs(self):
        return [MaintenanceSchedule()]


def _store_event(roles, dose_ms: str) -> AdminEvent:
    return AdminEvent(
        "store_consistency_strict", roles, component="redis/redis.cache-policy", hold_env={"STORE_HOLD_MS": dose_ms}
    )


def _acl_event(keep_active: bool = False) -> AdminEvent:
    return AdminEvent(
        "read_consistency_strict",
        ["channel"],
        component="channel/channel.membership-acl",
        hold_env={"ACL_HOLD_MS": "350"},
        keep_active=keep_active,
    )


class SlackLoginsUnreadSendsAllSlow(SlackSpineProblem):
    """Incident Arena 009: shared Redis store put into strict mode (250ms hold) on three services."""

    TASK = "009--slack-spine--06-logins-unread-and-sends-all-slow-since-noon-16e3fd23"
    PROBLEM_ID = "incident_arena_slack_logins_unread_sends_all_slow"

    def build_legs(self):
        return [_store_event(["auth", "workspace", "notification"], "250")]


class SlackLoginsUnreadSendsSlower(SlackSpineProblem):
    """Incident Arena 010: the same shared-store strict mode at a milder 100ms hold."""

    TASK = "010--slack-spine--06-logins-unread-sends-slower-cccddfb2"
    PROBLEM_ID = "incident_arena_slack_logins_unread_sends_slower"

    def build_legs(self):
        return [_store_event(["auth", "workspace", "notification"], "100")]


class SlackSendsCrawlThenStoreSlows(SlackSpineProblem):
    """Incident Arena 011: channel strict-ACL toggle + undersized channel pool + auth store strict mode."""

    TASK = "011--slack-spine--06-sends-crawl-then-store-slows-3e65c480"
    PROBLEM_ID = "incident_arena_slack_sends_crawl_then_store_slows"

    def build_legs(self):
        return [_acl_event(), _store_event(["auth"], "250"), RolePoolSize("channel", 3, 2)]


class SlackSendsFailStrictModePlausiblePool(SlackSpineProblem):
    """Incident Arena 012: channel strict-ACL toggle + a plausible-looking 12/2 channel pool."""

    TASK = "012--slack-spine--06-sends-fail-after-strict-mode-plausible-pool-d18e8739"
    PROBLEM_ID = "incident_arena_slack_sends_fail_strict_mode_plausible_pool"

    def build_legs(self):
        return [_acl_event(), RolePoolSize("channel", 12, 2)]


class SlackSendsFailStrictMode(SlackSpineProblem):
    """Incident Arena 013: channel strict-ACL toggle + a 3/2 channel pool."""

    TASK = "013--slack-spine--06-sends-fail-after-strict-mode-turns-on-728f0739"
    PROBLEM_ID = "incident_arena_slack_sends_fail_strict_mode"

    def build_legs(self):
        return [_acl_event(), RolePoolSize("channel", 3, 2)]


class SlackSendsFailComplianceWindow(SlackSpineProblem):
    """Incident Arena 014: the strict-ACL window is mandated; only the 3/2 channel pool may change."""

    TASK = "014--slack-spine--06-sends-fail-during-compliance-window-1f4b1235"
    PROBLEM_ID = "incident_arena_slack_sends_fail_compliance_window"

    def build_legs(self):
        # The mandated window is context, not a cause: only the pool is graded.
        return [_acl_event(keep_active=True), RolePoolSize("channel", 3, 2)]


class SlackSendsFailStrictPool16(SlackSpineProblem):
    """Incident Arena 015: channel strict-ACL toggle + a nominally sufficient 16/4 channel pool."""

    TASK = "015--slack-spine--06-sends-fail-strict-pool-16-66fa1a7c"
    PROBLEM_ID = "incident_arena_slack_sends_fail_strict_pool_16"

    def build_legs(self):
        return [_acl_event(), RolePoolSize("channel", 16, 4)]


class SlackSendsSlowAndStallEveryMinute(SlackSpineProblem):
    """Incident Arena 016: maintenance collision + undersized message pool."""

    TASK = "016--slack-spine--06-sends-slow-and-stall-every-minute-83867383"
    PROBLEM_ID = "incident_arena_slack_sends_slow_and_stall_every_minute"

    def build_legs(self):
        return [MaintenanceSchedule(), RolePoolSize("message", 3, 2, floors=None)]


class SlackStallEveryMinuteThenCrawl(SlackSpineProblem):
    """Incident Arena 017: maintenance collision + channel strict-ACL toggle (healthy pool)."""

    TASK = "017--slack-spine--06-stall-every-minute-and-later-every-send-crawls-bd50bae9"
    PROBLEM_ID = "incident_arena_slack_stall_every_minute_then_crawl"

    def build_legs(self):
        return [MaintenanceSchedule(), _acl_event()]


class SlackSeqLockLeak(SlackSpineProblem):
    """Incident Arena 018: the release leaks a chan-0 channel_seq row lock (idle in transaction)."""

    TASK = "018--slack-spine--09-I1-seq-lock-leak-0b7c2973"
    PROBLEM_ID = "incident_arena_slack_seq_lock_leak"
    # The reference repair bounds idle transactions server-wide.
    ALLOWED_PG_SETTINGS = ("idle_in_transaction_session_timeout",)

    def build_legs(self):
        return [LeakedSequencerLock()]


class SlackDistractorVolumeSeqLock(SlackSpineProblem):
    """Incident Arena 019: session-scoped sequencer handoff leaks a cohort lock under heavy log noise."""

    TASK = "019--slack-spine--13-P1-distractor-volume-shell-f73987d7"
    PROBLEM_ID = "incident_arena_slack_distractor_volume_seq_lock"

    def build_legs(self):
        return [SessionHandoffLock()]


__all__ = [
    "SlackSplitSequencer",
    "SlackMaintenanceCollision",
    "SlackLoginsUnreadSendsAllSlow",
    "SlackLoginsUnreadSendsSlower",
    "SlackSendsCrawlThenStoreSlows",
    "SlackSendsFailStrictModePlausiblePool",
    "SlackSendsFailStrictMode",
    "SlackSendsFailComplianceWindow",
    "SlackSendsFailStrictPool16",
    "SlackSendsSlowAndStallEveryMinute",
    "SlackStallEveryMinuteThenCrawl",
    "SlackSeqLockLeak",
    "SlackDistractorVolumeSeqLock",
]
