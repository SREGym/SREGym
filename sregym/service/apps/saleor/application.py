"""Saleor e-commerce stack from Incident Arena (PostgreSQL + Valkey + RabbitMQ + Celery)."""

from __future__ import annotations

import shlex
from pathlib import Path

from sregym.service.apps.components.incident_arena import IncidentArenaApplication


class Saleor(IncidentArenaApplication):
    app_id = "saleor"
    metadata_file = Path(__file__).with_name("metadata.json")
    values_file = Path(__file__).with_name("values.yaml")
    CHART_NAME = "saleor"
    # Migrations + populatedb run in the saleor-init Job before the API is ready.
    READY_TIMEOUT_S = 1800
    # A checkout makes at most five HTTP calls with ten-second timeouts.
    WORKLOAD_COMPLETION_TIMEOUT_S = 60.0

    POSTGRES_STATEFULSET = "postgres"
    ADMIN_ROLE = "saleoradmin"
    ADMIN_PASSWORD = "agentrepair-admin"
    APP_ROLE = "saleor_app"
    APP_PASSWORD = "agentrepair-app"
    DATABASE = "saleor"
    API_DEPLOYMENT = "saleor-api"

    def __init__(self):
        super().__init__(self.metadata_file)
        self.frontend_service = "svc-saleor-api"
        self.frontend_port = 8000

    def bootstrap_values(self) -> dict:
        """Do not start database consumers until migration and seeding finish."""
        return {"saleor": {role: {"replicaCount": 0} for role in ("api", "worker")}}

    def psql(self, sql: str, user: str | None = None, password: str | None = None, timeout: float = 60) -> str:
        """Run SQL over TCP inside the PostgreSQL pod; unaligned, tuples only."""
        user = user or self.ADMIN_ROLE
        password = password or self.ADMIN_PASSWORD
        command = (
            f"PGPASSWORD={shlex.quote(password)} psql -h 127.0.0.1 -U {shlex.quote(user)} -d {self.DATABASE} "
            f"-v ON_ERROR_STOP=1 -tA -c {shlex.quote(sql)}"
        )
        return self.exec_in(f"sts/{self.POSTGRES_STATEFULSET}", command, container="postgres", timeout=timeout)
