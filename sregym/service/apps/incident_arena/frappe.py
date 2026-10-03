"""Frappe/ERPNext site from Incident Arena (MariaDB + Redis cache/queue + RQ worker)."""

from __future__ import annotations

import shlex

from sregym.paths import FRAPPE_METADATA
from sregym.service.apps.incident_arena.base import IncidentArenaApplication


class Frappe(IncidentArenaApplication):
    CHART_NAME = "frappe"
    # `bench new-site` runs inside the install and can take 15-30 minutes on a
    # contended node (Incident Arena budgets 45 minutes for the build).
    READY_TIMEOUT_S = 2700

    MARIADB_STATEFULSET = "frappe-mariadb-subchart"
    MARIADB_ROOT_PASSWORD = "sre-world-mariadb-root"
    REDIS_QUEUE_STATEFULSET = "frappe-redis-queue-master"
    REDIS_QUEUE_POD = "frappe-redis-queue-master-0"
    REDIS_QUEUE_SCRIPTS = "frappe-redis-queue-scripts"
    REDIS_CACHE_STATEFULSET = "frappe-redis-cache-master"
    RQ_LONG_QUEUE = "rq:queue:home-frappe-frappe-bench:long"
    SITE_NAME = "svc-frappe-web"
    GUNICORN_DEPLOYMENT = "erp-gunicorn"
    WORKER_DEPLOYMENT = "erp-worker-l"

    def __init__(self):
        super().__init__(FRAPPE_METADATA)
        self.frontend_service = "svc-frappe-web"
        self.frontend_port = 8000

    # ------------------------------------------------------------------ data-plane helpers
    def mysql(self, sql: str, timeout: float = 60) -> str:
        """Run SQL as MariaDB root inside the database pod; tab-separated, no headers."""
        command = f"mysql -uroot -p{shlex.quote(self.MARIADB_ROOT_PASSWORD)} -N -B -e {shlex.quote(sql)}"
        return self.exec_in(f"sts/{self.MARIADB_STATEFULSET}", command, container="mariadb", timeout=timeout)

    def redis(self, *args: str, cache: bool = False, timeout: float = 60) -> str:
        """Run a redis-cli command against the queue (or cache) broker."""
        statefulset = self.REDIS_CACHE_STATEFULSET if cache else self.REDIS_QUEUE_STATEFULSET
        command = "redis-cli --no-auth-warning " + " ".join(shlex.quote(a) for a in args)
        return self.exec_in(f"sts/{statefulset}", command, container="redis", timeout=timeout)

    def site_python(self, body: str, timeout: float = 300) -> str:
        """Run ``body`` with a connected Frappe site (as Administrator) in the gunicorn pod.

        ``body`` sees ``frappe`` imported and connected; it should print its
        result and commit what it writes.
        """
        source = (
            "import frappe\n"
            f"frappe.init(site={self.SITE_NAME!r}, sites_path='.')\n"
            "frappe.connect()\n"
            "frappe.set_user('Administrator')\n"
            "try:\n"
            + "".join(f"    {line}\n" for line in body.strip().splitlines())
            + "finally:\n    frappe.destroy()\n"
        )
        command = "cd /home/frappe/frappe-bench/sites && ../env/bin/python -"
        return self.exec_in(f"deploy/{self.GUNICORN_DEPLOYMENT}", command, input_data=source, timeout=timeout)
