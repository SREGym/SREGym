"""Events for Slack Spine."""

from __future__ import annotations

from sregym.conductor.oracles.incident_arena import CheckResult
from sregym.conductor.problems.apps.slack_spine.components import SlackLeg
from sregym.service.apps.slack_spine.application import SlackSpine


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
