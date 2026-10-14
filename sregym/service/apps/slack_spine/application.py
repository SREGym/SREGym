"""Slack-like polyglot system from Incident Arena (slack-spine substrate)."""

from __future__ import annotations

import json
import shlex
from pathlib import Path

from sregym.service.apps.components.incident_arena import IncidentArenaApplication
from sregym.service.apps.helpers import image_reference

APP_ROLES = ("auth", "workspace", "channel", "message", "thread", "file", "search", "notification", "platform")


class SlackSpine(IncidentArenaApplication):
    app_id = "slack-spine"
    metadata_file = Path(__file__).with_name("metadata.json")
    values_file = Path(__file__).with_name("values.yaml")
    CHART_NAME = "slack-spine"
    READY_TIMEOUT_S = 1500
    # One send plus twenty ten-second search polls and 250ms between polls.
    WORKLOAD_COMPLETION_TIMEOUT_S = 220.0

    DB_STATEFULSET = "db"
    ADMIN_DSN = "postgresql://svc_admin:svc_admin@127.0.0.1:5432/app"
    APP_CONFIG_MAP = "app-config"
    MAINTENANCE_URL = "http://db-maintenance:8081/v1/maintenance"

    def __init__(self):
        super().__init__(self.metadata_file)
        self.configure(
            {
                "images": {
                    "app": image_reference("slack-spine-app"),
                    "appBuilder": image_reference("slack-spine-builder"),
                    "go": image_reference("slack-spine-go"),
                    "py": image_reference("slack-spine-python"),
                    "main": image_reference("slack-spine-toolbox"),
                    "loadgen": image_reference("slack-spine-loadgen"),
                    "pgbouncer": image_reference("slack-spine-pgbouncer"),
                    "minio": image_reference("slack-spine-minio"),
                }
            }
        )
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
