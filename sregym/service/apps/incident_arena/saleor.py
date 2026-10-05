"""Saleor e-commerce stack from Incident Arena (PostgreSQL + Valkey + RabbitMQ + Celery)."""

from __future__ import annotations

import shlex

from sregym.paths import SALEOR_METADATA
from sregym.service.apps.incident_arena.base import IncidentArenaApplication


class Saleor(IncidentArenaApplication):
    CHART_NAME = "saleor"
    # Migrations + populatedb run in the saleor-init Job before the API is ready.
    READY_TIMEOUT_S = 1800

    POSTGRES_STATEFULSET = "postgres"
    ADMIN_ROLE = "saleoradmin"
    ADMIN_PASSWORD = "agentrepair-admin"
    APP_ROLE = "saleor_app"
    APP_PASSWORD = "agentrepair-app"
    DATABASE = "saleor"
    API_DEPLOYMENT = "saleor-api"

    def __init__(self):
        super().__init__(SALEOR_METADATA)
        self.frontend_service = "svc-saleor-api"
        self.frontend_port = 8000

    # The API and worker start while the init Job is still migrating, and the
    # early "does not exist" errors stay in Loki. Hold both at zero until then.
    NOISE_ABLATION_VALUES = {"saleor": {"api": {"replicaCount": 0}, "worker": {"replicaCount": 0}}}

    def remove_runtime_noise(self) -> None:
        """Start the API and worker only after migrations, through Helm so its values stay consistent."""
        self.configure({"saleor": {"api": {"replicaCount": 1}, "worker": {"replicaCount": 1}}})
        overrides = self._write_overrides()
        cfg = self.helm_configs
        self.kubectl.exec_command_checked(
            f"helm upgrade {cfg['release_name']} {cfg['chart_path']} -n {self.namespace} "
            f"-f {self.values_file} -f {overrides}",
            timeout=600,
        )

    def psql(self, sql: str, user: str | None = None, password: str | None = None, timeout: float = 60) -> str:
        """Run SQL over TCP inside the PostgreSQL pod; unaligned, tuples only."""
        user = user or self.ADMIN_ROLE
        password = password or self.ADMIN_PASSWORD
        command = (
            f"PGPASSWORD={shlex.quote(password)} psql -h 127.0.0.1 -U {shlex.quote(user)} -d {self.DATABASE} "
            f"-v ON_ERROR_STOP=1 -tA -c {shlex.quote(sql)}"
        )
        return self.exec_in(f"sts/{self.POSTGRES_STATEFULSET}", command, container="postgres", timeout=timeout)
