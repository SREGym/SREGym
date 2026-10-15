"""Pools for Slack Spine."""

from __future__ import annotations

import json
import shlex

from sregym.conductor.oracles.incident_arena import CheckResult
from sregym.conductor.problems.apps.slack_spine.components import SlackLeg
from sregym.service.apps.slack_spine.configuration import (
    HEALTHY_HOLD_MS,
    HEALTHY_POOL,
    POOL_CEILING,
    POOL_TIMEOUT_FLOOR_S,
    patch_role_db_config,
)


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
