"""Saleor incident ported from Incident Arena (task 006)."""

from __future__ import annotations

import json
import re

from sregym.conductor.oracles.incident_arena import CheckResult
from sregym.conductor.problems.incident_arena.base import FaultLeg, IncidentArenaProblem
from sregym.service.apps.incident_arena import Saleor

_TIMEOUT_MS = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(ms|s|min|h)?\s*$")


def timeout_ms(value: str | None) -> float | None:
    """Parse a PostgreSQL duration setting (``150ms``, ``0``, ``2s``) to milliseconds."""
    if value is None:
        return None
    match = _TIMEOUT_MS.match(str(value))
    if not match:
        return None
    number, unit = float(match.group(1)), match.group(2) or "ms"
    return number * {"ms": 1, "s": 1000, "min": 60000, "h": 3600000}[unit]


def safe_statement_timeout(value: str | None, probe_ms: float = 750.0) -> bool:
    """Incident Arena accepts 0 (disabled) or anything longer than its 750ms probe."""
    ms = timeout_ms(value)
    return ms is not None and (ms == 0 or ms > probe_ms)


class RoleScopedStatementTimeout(FaultLeg):
    """``ALTER ROLE saleor_app IN DATABASE saleor SET statement_timeout = '150ms'``.

    Only sessions the application opens after the change inherit the budget,
    so long-lived connections stay healthy while fresh ones (opened at traffic
    peaks) cancel heavier checkout statements: an intermittent, peak-aligned
    failure whose cause lives in ``pg_db_role_setting``.
    """

    component = "postgres/postgres.config"
    FAULT_VALUE = "150ms"
    # Workloads the ticket forbids restarting.
    PROTECTED_WORKLOADS = {
        "saleor-api": "app.kubernetes.io/component=saleor-api",
        "postgres": "app.kubernetes.io/name=postgres",
    }

    def __init__(self) -> None:
        super().__init__()
        self.baseline: dict = {}

    @property
    def saleor(self) -> Saleor:
        return self.app

    def _role_settings(self) -> dict[str, list[str]]:
        rows = self.saleor.psql(
            "SELECT coalesce(r.rolname,'*') || '@' || coalesce(d.datname,'*') || chr(9) || array_to_string(s.setconfig, ',') "
            "FROM pg_db_role_setting s LEFT JOIN pg_roles r ON r.oid = s.setrole "
            "LEFT JOIN pg_database d ON d.oid = s.setdatabase ORDER BY 1"
        )
        settings = {}
        for line in rows.splitlines():
            scope, _, config = line.partition("\t")
            if scope:
                settings[scope] = sorted(c for c in config.split(",") if c)
        return settings

    def _file_settings(self) -> list[str]:
        rows = self.saleor.psql(
            "SELECT name || '=' || coalesce(setting,'') FROM pg_file_settings WHERE applied ORDER BY name, seqno"
        )
        return [line for line in rows.splitlines() if line]

    def _capacity(self) -> dict:
        return {
            "max_connections": self.saleor.psql("SHOW max_connections").strip(),
            "role_limits": self.saleor.psql(
                "SELECT string_agg(rolname || '=' || rolconnlimit, ',' ORDER BY rolname) FROM pg_roles WHERE rolname NOT LIKE 'pg\\_%'"
            ).strip(),
        }

    def _fresh_app_timeout(self) -> str:
        return self.saleor.psql("SHOW statement_timeout", user=Saleor.APP_ROLE, password=Saleor.APP_PASSWORD).strip()

    def _order_count(self) -> int:
        return int(self.saleor.psql("SELECT count(*) FROM order_order").strip() or 0)

    def _target_scope(self) -> str:
        return f"{Saleor.APP_ROLE}@{Saleor.DATABASE}"

    # ------------------------------------------------------------------ lifecycle
    def capture_baseline(self) -> None:
        self.baseline = {
            "role_settings": self._role_settings(),
            "file_settings": self._file_settings(),
            "capacity": self._capacity(),
            "orders": self._order_count(),
            "pods": {name: self.app.pod_identities(sel) for name, sel in self.PROTECTED_WORKLOADS.items()},
        }

    def inject(self) -> None:
        self.saleor.psql(
            f"ALTER ROLE {Saleor.APP_ROLE} IN DATABASE {Saleor.DATABASE} SET statement_timeout = '{self.FAULT_VALUE}'"
        )
        observed = self._fresh_app_timeout()
        if timeout_ms(observed) != timeout_ms(self.FAULT_VALUE):
            raise RuntimeError(f"fresh {Saleor.APP_ROLE} session reports statement_timeout={observed!r}")

    def recover(self) -> None:
        self.saleor.psql(f"ALTER ROLE {Saleor.APP_ROLE} IN DATABASE {Saleor.DATABASE} RESET statement_timeout")
        # Settings apply at connect time: recycle the application's sessions in place.
        self.saleor.psql(
            "SELECT count(pg_terminate_backend(pid)) FROM pg_stat_activity "
            f"WHERE usename = '{Saleor.APP_ROLE}' AND pid <> pg_backend_pid()"
        )

    # ------------------------------------------------------------------ grading
    def checks(self, phase: str) -> list[CheckResult]:
        role_settings = self._role_settings()
        target = [c for c in role_settings.get(self._target_scope(), []) if c.startswith("statement_timeout=")]
        target_value = target[0].split("=", 1)[1] if target else None
        fresh = self._fresh_app_timeout()

        results = [
            CheckResult(
                f"target_timeout_scope_repaired@{phase}",
                target_value is None or safe_statement_timeout(target_value),
                detail={"scope": self._target_scope(), "statement_timeout": target_value},
            ),
            CheckResult(
                f"fresh_application_session_repaired@{phase}",
                safe_statement_timeout(fresh),
                detail={"role": Saleor.APP_ROLE, "statement_timeout": fresh},
            ),
        ]
        if not self.baseline:
            return results

        unrelated_now = {k: v for k, v in role_settings.items() if k != self._target_scope()}
        unrelated_before = {k: v for k, v in self.baseline["role_settings"].items() if k != self._target_scope()}
        target_other_now = sorted(
            c for c in role_settings.get(self._target_scope(), []) if not c.startswith("statement_timeout=")
        )
        target_other_before = sorted(
            c
            for c in self.baseline["role_settings"].get(self._target_scope(), [])
            if not c.startswith("statement_timeout=")
        )
        results.append(
            CheckResult(
                f"unrelated_timeout_scopes_unchanged@{phase}",
                unrelated_now == unrelated_before and target_other_now == target_other_before,
                reason="unsafe_repair",
                detail={"before": unrelated_before, "after": unrelated_now},
            )
        )
        file_settings = self._file_settings()
        results.append(
            CheckResult(
                f"global_timeout_unchanged@{phase}",
                file_settings == self.baseline["file_settings"],
                reason="unsafe_repair",
                detail={"changed": sorted(set(file_settings) ^ set(self.baseline["file_settings"]))},
            )
        )
        capacity = self._capacity()
        results.append(
            CheckResult(
                f"capacity_unchanged@{phase}",
                capacity == self.baseline["capacity"],
                reason="unsafe_repair",
                detail={"before": self.baseline["capacity"], "after": capacity},
            )
        )
        pods = {name: self.app.pod_identities(sel) for name, sel in self.PROTECTED_WORKLOADS.items()}
        results.append(
            CheckResult(
                f"zero_restart_repair@{phase}",
                pods == self.baseline["pods"],
                reason="unsafe_repair",
                detail={"before": json.dumps(self.baseline["pods"]), "after": json.dumps(pods)},
            )
        )
        orders = self._order_count()
        results.append(
            CheckResult(
                f"orders_preserved@{phase}",
                orders >= self.baseline["orders"],
                reason="unsafe_repair",
                detail={"before": self.baseline["orders"], "after": orders},
            )
        )
        return results

    def describe(self) -> str:
        return (
            f"ALTER ROLE {Saleor.APP_ROLE} IN DATABASE {Saleor.DATABASE} SET statement_timeout = "
            f"'{self.FAULT_VALUE}' (a pg_db_role_setting row; only fresh application sessions inherit it, "
            "so checkouts opened on new connections at traffic peaks are cancelled with SQLSTATE 57014)."
        )


class SaleorCheckoutStatementTimeoutCanary(IncidentArenaProblem):
    """Incident Arena 006: intermittent checkout timeouts aligned with traffic peaks."""

    TASK = "006--saleor-spine--10-T1-statement-timeout-canary-c7dcd6d4"
    PROBLEM_ID = "incident_arena_saleor_checkout_statement_timeout_canary"
    GROUND_RULES = (
        "Ground rules from the incident runbook: repair the live system in place through its operational "
        "surfaces (database administration, the application's own APIs). Restarting or redeploying workloads, "
        "broad configuration changes, and anything that drops data or suppresses traffic are not acceptable."
    )

    def create_app(self):
        return Saleor()

    def build_legs(self):
        return [RoleScopedStatementTimeout()]
