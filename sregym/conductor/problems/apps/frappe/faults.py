"""Frappe database and queue fault components with their recovery checks."""

from __future__ import annotations

import json
import shlex
import time
from collections.abc import Callable

from sregym.conductor.oracles.incident_arena import CheckResult
from sregym.conductor.problems.contracts.incident_arena.base import FaultLeg
from sregym.service.apps.frappe.application import Frappe
from sregym.service.apps.frappe.operations import (
    DEQUEUE_COMMANDS,
    HEALTHY_QUEUE_FLAGS,
    MAXMEMORY_BAND,
    global_variables,
    redis_acl_rules,
    redis_config,
    render_start_script,
    schema_privileges,
    site_account,
    site_database,
)


class FrappeLeg(FaultLeg):
    @property
    def frappe(self) -> Frappe:
        return self.app


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
