"""Operator heartbeat DAG: a short pipeline that runs every minute.

The platform team relies on these runs completing every minute. When the
scheduler stops moving tasks out of the ``scheduled`` state, these runs pile up
and never finish, which the fleet monitor reports as a user-facing failure.
"""

from __future__ import annotations

from datetime import datetime

from airflow import DAG
from airflow.providers.standard.operators.bash import BashOperator

with DAG(
    dag_id="fleet_heartbeat",
    schedule="*/1 * * * *",
    start_date=datetime(2024, 1, 1),
    catchup=False,
    max_active_runs=1,
    default_args={"retries": 0, "priority_weight": 100},
    tags=["fleet"],
) as dag:
    collect = BashOperator(task_id="collect", bash_command="echo collect ok")
    publish = BashOperator(task_id="publish", bash_command="echo publish ok")
    collect >> publish
