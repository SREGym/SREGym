"""Frappe: workers."""

from __future__ import annotations

import json

from sregym.conductor.oracles.dev_shm_mitigation_oracle import DevShmMitigationOracle
from sregym.conductor.oracles.mitigation import MitigationOracle
from sregym.conductor.problems.apps.frappe.helpers import WEB_SERVICE
from sregym.service.apps.frappe.operations import background_job_probe


class DevShmFrappeOracle(DevShmMitigationOracle):
    """``DevShmMitigationOracle`` that also accepts removing the oversized staging step.

    In the original the shared-memory need is a brand-new workload's, so only a
    bigger /dev/shm fixes it. Here it arrives as a worker template change with
    rollout history, so rolling that change back is an equally valid repair.
    """

    def _has_memory_backed_shm(self, pod_spec) -> bool:
        if super()._has_memory_backed_shm(pod_spec):
            return True
        marker = f"{self.problem.shm_mount_path}/rq-spool"
        return not any(marker in " ".join([*(c.command or []), *(c.args or [])]) for c in pod_spec.containers or [])


class FrappeWorkerHealthOracle(MitigationOracle):
    """Check required workloads, the web API, and new background work."""

    def evaluate(self) -> dict:
        health = super().evaluate()
        if not health["success"]:
            return health
        try:
            code, body = self.problem.app.http(f"http://{WEB_SERVICE}:8000/api/method/ping")
            if code != 200 or json.loads(body).get("message") != "pong":
                return self.fail("application_probe_failed", status=code)
            job = background_job_probe(self.problem.app)
        except Exception as exc:
            return self.fail_from_exception(exc)
        print(f"Background worker check: {job}")
        return {"success": True, "background_job": job} if job["ok"] else self.fail("background_job_failed", **job)
