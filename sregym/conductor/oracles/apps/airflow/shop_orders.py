"""Shared pieces of the Airflow shop-deployment mitigation oracles.

``_ShopOracle`` separates the verdict ("service restored and damage repaired",
which decides pass/fail) from ``collateral`` (reported as its own field, never a
failure on its own unless it caused harm). Every check runs and is reported in
``checks``, even after an earlier one failed; the verdict's ``reason`` is the
first failed check in the documented order. ``pod_snapshot`` and
``restarted_since`` record which components restarted after handover.
"""

from __future__ import annotations

import json
import logging

from sregym.conductor.oracles.base import Oracle

logger = logging.getLogger(__name__)

#: Components whose pods the collateral report tracks (label selectors).
COMPONENTS = {
    "scheduler": "component=scheduler,release=airflow",
    "triggerer": "component=triggerer,release=airflow",
    "worker": "component=worker,release=airflow",
    "dag-processor": "component=dag-processor,release=airflow",
    "api-server": "component=api-server,release=airflow",
    "redis": "component=redis,release=airflow",
    "pgbouncer": "component=pgbouncer,release=airflow",
    "postgres": "app=postgres",
    "orders-sqs": "app=orders-sqs",
    "storefront": "app=storefront",
    "ops-notify": "app=ops-notify",
}


def pod_snapshot(app) -> dict:
    """{component: {pod uid: restart count}} for the tracked components."""
    snapshot = {}
    for component, selector in COMPONENTS.items():
        pods = json.loads(app.command("get", "pods", "-l", selector, "-o", "json", timeout=30))["items"]
        snapshot[component] = {
            p["metadata"]["uid"]: sum(
                cs.get("restartCount", 0) for cs in p.get("status", {}).get("containerStatuses", [])
            )
            for p in pods
            if not p["metadata"].get("deletionTimestamp")
        }
    return snapshot


def restarted_since(app, before: dict | None) -> list[str]:
    if not before:
        return []
    now = pod_snapshot(app)
    changed = []
    for component, pods in now.items():
        old = before.get(component, {})
        if set(pods) != set(old) or any(pods[uid] > old.get(uid, 0) for uid in pods):
            changed.append(component)
    return changed


class _ShopOracle(Oracle):
    def _replicas(self, kind: str, name: str) -> int:
        out = self.problem.app.command(
            "get", kind, name, "--ignore-not-found", "-o", "jsonpath={.spec.replicas}", timeout=30
        ).strip()
        return int(out) if out else 0

    def _check(self, checks: dict, name: str, fn) -> None:
        try:
            checks[name] = fn()
        except Exception as error:  # noqa: BLE001 - a probe error is reported, not raised
            logger.exception("oracle check %s raised", name)
            checks[name] = {"ok": None, "error": f"{type(error).__name__}: {error}"}

    def _verdict(self, checks: dict, order: list[tuple[str, str]], collateral: dict) -> dict:
        for name, reason in order:
            if checks.get(name, {}).get("ok") is False:
                verdict = self.fail(reason, check=name)
                break
        else:
            unknown = [name for name, _ in order if checks.get(name, {}).get("ok") is None]
            verdict = self.fail("probe_failed", checks=unknown) if unknown else {"success": True}
        verdict["checks"] = checks
        verdict["collateral"] = collateral
        print(
            f"[airflow oracle] verdict={verdict.get('success')} reason={verdict.get('reason')} "
            f"collateral={json.dumps(collateral, default=str)}"
        )
        for name, result in checks.items():
            print(f"[airflow oracle]   {name}: {json.dumps(result, default=str)[:600]}")
        return verdict

    def _dag_check(self, dag_id: str) -> dict:
        app = self.problem.app
        row = app.psql(f"SELECT is_paused FROM dag WHERE dag_id='{dag_id}'").strip()
        return {"ok": row == "f", "is_paused": row or "missing"}
