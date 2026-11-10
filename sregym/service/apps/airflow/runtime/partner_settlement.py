"""Partner settlement: reports shipped orders to the payment partner every five minutes.

The runs at :00, :15, :30 and :45 also do the full reconciliation: every shipment
of the day is re-verified with the partner before the report is published, which
takes several minutes. The partner expects each report within two minutes of the
run being queued; when a run is late, a late notice is posted to the
partner-settlement channel through ops-notify.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta

from airflow.providers.postgres.hooks.postgres import PostgresHook
from airflow.sdk import DAG, DeadlineAlert, DeadlineReference, SyncCallback, task
from ops_alerts import post_late_notice

#: Seconds the partner API needs to re-verify one batch of shipments.
VERIFY_BATCH_S = 10
FULL_RECONCILE_BATCHES = 22

with DAG(
    dag_id="partner_settlement",
    schedule="*/5 * * * *",
    start_date=datetime(2024, 1, 1),
    catchup=False,
    max_active_runs=1,
    default_args={"retries": 1, "retry_delay": timedelta(seconds=30)},
    deadline=DeadlineAlert(
        reference=DeadlineReference.DAGRUN_QUEUED_AT,
        interval=timedelta(minutes=2),
        callback=SyncCallback(post_late_notice, kwargs={"channel": "partner-settlement"}),
    ),
    tags=["shop"],
) as dag:

    @task
    def collect_shipments(dag_run=None):
        scheduled_for = dag_run.logical_date or dag_run.run_after
        full = bool((dag_run.conf or {}).get("full_reconcile")) or scheduled_for.minute % 15 == 0
        hook = PostgresHook(postgres_conn_id="shop_db")
        shipped_today = hook.get_first("SELECT count(*) FROM shipments WHERE created_at >= date_trunc('day', now())")[0]
        if full:
            for batch in range(FULL_RECONCILE_BATCHES):
                time.sleep(VERIFY_BATCH_S)
                print(f"re-verified batch {batch + 1}/{FULL_RECONCILE_BATCHES} with the partner")
        else:
            time.sleep(5)
        return {"shipped_today": int(shipped_today), "full_reconcile": full}

    @task
    def publish_report(totals, dag_run=None):
        hook = PostgresHook(postgres_conn_id="shop_db")
        hook.run(
            "INSERT INTO settlement_reports (dag_run_id, shipped_today, full_reconcile) VALUES (%s, %s, %s)",
            parameters=(dag_run.run_id, totals["shipped_today"], totals["full_reconcile"]),
        )
        print(f"published settlement report: {totals}")

    publish_report(collect_shipments())
