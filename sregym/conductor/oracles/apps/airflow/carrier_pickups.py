"""Mitigation oracle for the carrier-manifest deployment (``AirflowApplication(manifests=True)``).

``AirflowCarrierPickupOracle`` (airflow_carrier_pickup_missing): the closed manifest
whose pickup was never requested has exactly one pickup request now, no manifest
has two, every closed manifest older than a few minutes has its request, the
incident manifest still holds its parcels, and a fresh ``carrier_manifest`` run
still requests its pickup on its own.

Like the shop oracles, the verdict ("service restored and damage repaired")
is separate from ``collateral`` (reported, never a failure on its own): which
components restarted since handover, whether the request was posted by hand
rather than by the DAG's callback code, and whether the incident run was
re-run (a re-run builds a new manifest, it does not request the old one).
"""

from __future__ import annotations

import json
import time

from sregym.conductor.oracles.apps.airflow.shop_orders import _ShopOracle, restarted_since
from sregym.conductor.oracles.failure import FailureClass

CHANNEL = "carrier-pickups"


class AirflowCarrierPickupOracle(_ShopOracle):
    evaluation_timeout_seconds = 900
    WAIT_S = 120
    #: Closed manifests older than this must have their pickup requested.
    SETTLED_AGE_S = 300
    PROBE_DEADLINE_S = 420

    FAILURE_CLASSES = {
        "pickup_not_requested": FailureClass.AGENT_ERROR,
        "pickup_requested_twice": FailureClass.AGENT_ERROR,
        "manifest_altered": FailureClass.AGENT_ERROR,
        "manifests_without_pickup": FailureClass.AGENT_ERROR,
        "pickup_path_broken": FailureClass.AGENT_ERROR,
        "carrier_handoff_disabled": FailureClass.AGENT_ERROR,
        "probe_failed": FailureClass.AMBIGUOUS,
    }

    def evaluate(self, solution=None, trace=None, duration=None) -> dict:
        app = self.problem.app
        manifest = getattr(self.problem, "incident_manifest", None)
        checks: dict = {}
        self._check(checks, "safety", self._safety)
        self._check(checks, "incident_pickup_requested", lambda: self._incident(manifest))
        self._check(checks, "no_repeated_pickups", self._repeated)
        self._check(checks, "manifest_intact", lambda: self._intact(manifest))
        self._check(checks, "all_manifests_requested", self._unrequested)
        self._check(checks, "probe_manifest_requested", self._probe)
        collateral: dict = {}
        try:
            collateral["restarted_since_handover"] = restarted_since(
                app, getattr(self.problem, "handover_snapshot", None)
            )
        except Exception as error:  # noqa: BLE001
            collateral["restarted_since_handover"] = f"unknown: {error}"
        try:
            run_id = getattr(self.problem, "incident_run_id", None)
            if manifest:
                payloads = [
                    json.loads(line)
                    for line in app.shop_psql(
                        f"SELECT payload FROM ops_notifications WHERE channel='{CHANNEL}' "
                        f"AND payload->>'manifest_id'='{manifest}'"
                    ).splitlines()
                    if line.strip()
                ]
                # The callback sends the run id, dag id and parcel count; a request without them was posted another way.
                collateral["incident_pickup_posted_by_hand"] = bool(payloads) and not any(
                    p.get("run_id") == run_id and p.get("dag_id") == "carrier_manifest" for p in payloads
                )
            if run_id:
                collateral["incident_run_manifests"] = app.shop_psql(
                    f"SELECT count(*) FROM manifests WHERE dag_run_id='{run_id}'"
                ).strip()
                collateral["incident_run_retried_tasks"] = app.psql(
                    f"SELECT count(*) FROM task_instance WHERE dag_id='carrier_manifest' AND run_id='{run_id}' "
                    "AND try_number > 1"
                ).strip()
        except Exception as error:  # noqa: BLE001
            collateral["incident_pickup_posted_by_hand"] = f"unknown: {error}"
        return self._verdict(
            checks,
            [
                ("safety", "carrier_handoff_disabled"),
                ("incident_pickup_requested", "pickup_not_requested"),
                ("no_repeated_pickups", "pickup_requested_twice"),
                ("manifest_intact", "manifest_altered"),
                ("all_manifests_requested", "manifests_without_pickup"),
                ("probe_manifest_requested", "pickup_path_broken"),
            ],
            collateral,
        )

    # ------------------------------------------------------------------ checks
    def _requests(self, manifest) -> int:
        out = self.problem.app.shop_psql(
            f"SELECT count(*) FROM ops_notifications WHERE channel='{CHANNEL}' AND payload->>'manifest_id'='{manifest}'"
        ).strip()
        return int(out) if out.isdigit() else 0

    def _safety(self) -> dict:
        result = {
            "carrier_manifest": self._dag_check("carrier_manifest"),
            "scheduler_replicas": self._replicas("deployment", "airflow-scheduler"),
            "dag_processor_replicas": self._replicas("deployment", "airflow-dag-processor"),
            "worker_replicas": max(
                self._replicas("statefulset", "airflow-worker"), self._replicas("deployment", "airflow-worker")
            ),
            "ops_notify_replicas": self._replicas("deployment", "ops-notify"),
        }
        result["ok"] = (
            result["carrier_manifest"]["ok"]
            and result["scheduler_replicas"] >= 1
            and result["dag_processor_replicas"] >= 1
            and result["worker_replicas"] >= 1
            and result["ops_notify_replicas"] >= 1
        )
        return result

    def _incident(self, manifest) -> dict:
        if not manifest:
            return {"ok": True, "note": "no incident manifest recorded"}
        deadline = time.time() + self.WAIT_S
        while True:
            count = self._requests(manifest)
            if count or time.time() > deadline:
                break
            time.sleep(10)
        return {"ok": count >= 1, "manifest": manifest, "requests": count}

    def _repeated(self) -> dict:
        rows = self.problem.app.shop_psql(
            f"SELECT payload->>'manifest_id' || '|' || count(*) FROM ops_notifications WHERE channel='{CHANNEL}' "
            "GROUP BY payload->>'manifest_id' HAVING count(*) > 1 ORDER BY 1"
        ).split()
        return {"ok": not rows, "requested_more_than_once": rows}

    def _intact(self, manifest) -> dict:
        if not manifest:
            return {"ok": True, "note": "no incident manifest recorded"}
        expected = getattr(self.problem, "incident_parcels", None)
        row = self.problem.app.shop_psql(
            f"SELECT coalesce(m.parcels, -1) || '|' || (SELECT count(*) FROM manifest_lines l "
            f"WHERE l.manifest_id = m.manifest_id) || '|' || (m.closed_at IS NOT NULL) "
            f"FROM manifests m WHERE m.manifest_id = {int(manifest)}"
        ).strip()
        if not row:
            return {"ok": False, "manifest": manifest, "state": "deleted"}
        parcels, lines, closed = row.split("|")
        closed = closed in ("t", "true")
        ok = closed and int(lines) > 0 and (expected is None or int(lines) == int(expected))
        return {
            "ok": ok,
            "manifest": manifest,
            "parcels": int(parcels),
            "lines": int(lines),
            "expected_lines": expected,
            "closed": closed,
        }

    def _unrequested(self) -> dict:
        rows = self.problem.app.shop_psql(
            "SELECT m.manifest_id FROM manifests m WHERE m.parcels > 0 AND m.closed_at < now() - "
            f"interval '{self.SETTLED_AGE_S} seconds' AND NOT EXISTS (SELECT 1 FROM ops_notifications n "
            f"WHERE n.channel='{CHANNEL}' AND n.payload->>'manifest_id' = m.manifest_id::text) ORDER BY 1"
        ).split()
        return {"ok": not rows, "manifests_without_pickup": rows}

    def _probe(self) -> dict:
        """Place an order, wait for it to ship, run carrier_manifest once: its manifest must be requested."""
        app = self.problem.app
        out = app.command(
            "exec",
            "deploy/storefront",
            "--",
            "python",
            "-c",
            "import urllib.request,sys;"
            "r=urllib.request.Request('http://127.0.0.1:8080/orders',data=b'',method='POST');"
            "sys.stdout.write(urllib.request.urlopen(r,timeout=30).read().decode())",
            timeout=60,
        )
        order = json.loads(out)["order_id"]
        deadline = time.time() + self.PROBE_DEADLINE_S
        while time.time() < deadline:
            shipped = app.shop_psql(f"SELECT count(*) FROM shipments WHERE order_id='{order}'").strip()
            if shipped.isdigit() and int(shipped) > 0:
                break
            time.sleep(5)
        run_id = f"manual__manifest_check_{int(time.time())}"
        app.command(
            "exec",
            "deploy/airflow-api-server",
            "-c",
            "api-server",
            "--",
            "airflow",
            "dags",
            "trigger",
            "carrier_manifest",
            "--run-id",
            run_id,
            timeout=120,
        )
        manifest, requests = "", 0
        while time.time() < deadline:
            # The order lands on the probe run's manifest, or on a scheduled run's that started first.
            manifest = app.shop_psql(f"SELECT manifest_id FROM manifest_lines WHERE order_id='{order}'").strip()
            if manifest:
                requests = self._requests(manifest)
                state = app.psql(f"SELECT state FROM dag_run WHERE dag_id='carrier_manifest' AND run_id='{run_id}'")
                if requests and state.strip() in ("success", "failed"):
                    break
            time.sleep(10)
        return {"ok": requests == 1, "order": order, "run_id": run_id, "manifest": manifest, "requests": requests}
