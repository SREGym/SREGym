"""Pickup requests to the carrier desk, relayed through ops-notify."""

from __future__ import annotations

import json
import urllib.request

from airflow.providers.postgres.hooks.postgres import PostgresHook

OPS_NOTIFY_URL = "http://ops-notify:8080/notify"
CHANNEL = "carrier-pickups"


def request_pickup(context=None, **_):
    """Request the carrier pickup for the manifest the finished run closed."""
    run_id = (context or {}).get("run_id")
    hook = PostgresHook(postgres_conn_id="shop_db")
    row = hook.get_first(
        "SELECT manifest_id, parcels FROM manifests WHERE dag_run_id = %s AND closed_at IS NOT NULL "
        "ORDER BY manifest_id DESC LIMIT 1",
        parameters=(run_id,),
    )
    if not row or not row[1]:
        print(f"no parcels to collect for {run_id}")
        return
    manifest_id, parcels = row
    notice = {
        "channel": CHANNEL,
        "dag_id": "carrier_manifest",
        "run_id": run_id,
        "manifest_id": manifest_id,
        "parcels": parcels,
        "text": f"pickup requested for manifest {manifest_id} ({parcels} parcels)",
    }
    request = urllib.request.Request(
        OPS_NOTIFY_URL,
        data=json.dumps(notice).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        response.read()
    print(notice["text"])
