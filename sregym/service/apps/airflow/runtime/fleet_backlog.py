"""Fleet ingest DAG: a wide fan-out of short tasks, triggered on demand.

Each run fans out into many short tasks. The platform triggers these runs for
batch ingest; a backlog of them is ordinary when the fleet is busy.
"""

from __future__ import annotations

from datetime import datetime

from airflow import DAG
from airflow.providers.standard.operators.bash import BashOperator

WIDTH = 80

with DAG(
    dag_id="fleet_backlog",
    schedule=None,
    start_date=datetime(2024, 1, 1),
    catchup=False,
    max_active_runs=16,
    default_args={"retries": 0},
    tags=["fleet"],
) as dag:
    for index in range(WIDTH):
        BashOperator(task_id=f"ingest_{index:03d}", bash_command="sleep 3")
