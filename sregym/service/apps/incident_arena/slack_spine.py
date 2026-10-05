"""Slack-like polyglot system from Incident Arena (slack-spine substrate)."""

from __future__ import annotations

import json
import shlex

from sregym.paths import SLACK_SPINE_METADATA
from sregym.service.apps.incident_arena.base import IncidentArenaApplication

APP_ROLES = ("auth", "workspace", "channel", "message", "thread", "file", "search", "notification", "platform")


class SlackSpine(IncidentArenaApplication):
    CHART_NAME = "slack-spine"
    READY_TIMEOUT_S = 1500

    DB_STATEFULSET = "db"
    ADMIN_DSN = "postgresql://svc_admin:svc_admin@127.0.0.1:5432/app"
    APP_CONFIG_MAP = "app-config"
    MAINTENANCE_URL = "http://db-maintenance:8081/v1/maintenance"
    # svc-message holds a DB connection for 150 ms by default; its peers hold 5-12 ms.
    NOISE_ABLATION_VALUES = {"app": {"roles": {"message": {"db": {"hold_ms": 10}}}}}

    def __init__(self):
        super().__init__(SLACK_SPINE_METADATA)
        self.frontend_service = "svc-message"
        self.frontend_port = 8000

    @staticmethod
    def role_url(role: str, path: str) -> str:
        return f"http://svc-{role}:8000{path}"

    @staticmethod
    def role_selector(role: str) -> str:
        return f"app.kubernetes.io/component=svc-{role}"

    def psql(self, sql: str, timeout: float = 60) -> str:
        """Run SQL as the privileged svc_admin role inside the db pod."""
        command = f"psql {shlex.quote(self.ADMIN_DSN)} -v ON_ERROR_STOP=1 -tA -c {shlex.quote(sql)}"
        return self.exec_in(f"sts/{self.DB_STATEFULSET}", command, container="postgres", timeout=timeout)

    def admin(self, role: str, path: str, method: str = "GET", body=None) -> dict:
        """Call a role's admin API through the toolbox and decode the JSON reply."""
        status, payload = self.http(self.role_url(role, path), method=method, body=body)
        if not 200 <= status < 300:
            raise RuntimeError(f"{method} svc-{role}{path} returned {status}: {payload[:300]}")
        return json.loads(payload) if payload.strip() else {}
