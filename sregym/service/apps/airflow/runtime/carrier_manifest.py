"""Carrier manifest: hands the shipped parcels to the carrier every five minutes.

Each run collects the shipments that are not on a manifest yet into a new
manifest, prints the parcel labels and closes the manifest. When the run
succeeds, a pickup is requested from the carrier desk for the closed manifest
(see ``carrier_desk.request_pickup``); the carrier collects only manifests it
received a pickup request for.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta

from airflow.providers.postgres.hooks.postgres import PostgresHook
from airflow.sdk import DAG, task
from carrier_desk import request_pickup

with DAG(
    dag_id="carrier_manifest",
    schedule="3-59/5 * * * *",
    start_date=datetime(2024, 1, 1),
    catchup=False,
    max_active_runs=1,
    default_args={"retries": 1, "retry_delay": timedelta(seconds=20)},
    on_success_callback=request_pickup,
    tags=["shop"],
) as dag:

    @task
    def build_manifest(dag_run=None):
        hook = PostgresHook(postgres_conn_id="shop_db")
        with hook.get_conn() as conn, conn.cursor() as cur:
            cur.execute("INSERT INTO manifests (dag_run_id) VALUES (%s) RETURNING manifest_id", (dag_run.run_id,))
            manifest_id = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO manifest_lines (manifest_id, order_id) "
                "SELECT %s, s.order_id FROM shipments s "
                "WHERE NOT EXISTS (SELECT 1 FROM manifest_lines l WHERE l.order_id = s.order_id) "
                "GROUP BY s.order_id ORDER BY min(s.created_at)",
                (manifest_id,),
            )
            parcels = cur.rowcount
            cur.execute("UPDATE manifests SET parcels = %s WHERE manifest_id = %s", (parcels, manifest_id))
            conn.commit()
        print(f"manifest {manifest_id}: {parcels} parcel(s)")
        return manifest_id

    @task
    def print_labels(manifest_id):
        hook = PostgresHook(postgres_conn_id="shop_db")
        orders = [
            row[0]
            for row in hook.get_records(
                "SELECT order_id FROM manifest_lines WHERE manifest_id = %s ORDER BY order_id",
                parameters=(manifest_id,),
            )
        ]
        for order in orders:
            time.sleep(1)
            print(f"label printed for {order}")
        return manifest_id

    @task
    def close_manifest(manifest_id):
        hook = PostgresHook(postgres_conn_id="shop_db")
        hook.run("UPDATE manifests SET closed_at = now() WHERE manifest_id = %s", parameters=(manifest_id,))
        print(f"manifest {manifest_id} closed")

    close_manifest(print_labels(build_manifest()))
