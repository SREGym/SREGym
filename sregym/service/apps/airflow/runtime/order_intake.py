"""Order intake: ships every storefront order published on the orders queue.

The storefront publishes each new order to the ``orders`` SQS queue. This DAG
runs continuously: it waits for the next batch of orders on the queue and
records one shipment per order in the shop database.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

from airflow.providers.amazon.aws.sensors.sqs import SqsSensor
from airflow.providers.postgres.hooks.postgres import PostgresHook
from airflow.sdk import DAG, task

ORDERS_QUEUE = "http://orders-sqs:9324/000000000000/orders"

with DAG(
    dag_id="order_intake",
    schedule="@continuous",
    start_date=datetime(2024, 1, 1),
    catchup=False,
    max_active_runs=1,
    default_args={"retries": 2, "retry_delay": timedelta(seconds=20)},
    tags=["shop"],
) as dag:
    wait_for_orders = SqsSensor(
        task_id="wait_for_orders",
        sqs_queue=ORDERS_QUEUE,
        aws_conn_id="aws_default",
        max_messages=10,
        wait_time_seconds=10,
        poke_interval=5,
        timeout=6 * 3600,
        deferrable=True,
    )

    @task
    def ship_orders(ti=None, dag_run=None):
        messages = ti.xcom_pull(task_ids="wait_for_orders", key="messages") or []
        shipped = []
        hook = PostgresHook(postgres_conn_id="shop_db")
        with hook.get_conn() as conn, conn.cursor() as cur:
            for message in messages:
                order = json.loads(message["Body"])
                cur.execute(
                    "INSERT INTO shipments (order_id, dag_run_id, sqs_message_id) VALUES (%s, %s, %s)",
                    (order["order_id"], dag_run.run_id, message["MessageId"]),
                )
                shipped.append(order["order_id"])
            conn.commit()
        print(f"shipped {len(shipped)} order(s): {', '.join(shipped)}")
        return shipped

    wait_for_orders >> ship_orders()
