"""Notices posted to the ops-notify relay (late pipeline runs)."""

from __future__ import annotations

import json
import urllib.request

OPS_NOTIFY_URL = "http://ops-notify:8080/notify"


def post_late_notice(channel, context=None, **_):
    run = (context or {}).get("dag_run") or {}
    deadline = (context or {}).get("deadline") or {}
    notice = {
        "channel": channel,
        "dag_id": run.get("dag_id"),
        "run_id": run.get("dag_run_id") or run.get("run_id"),
        "deadline_id": deadline.get("id"),
        "deadline_time": str(deadline.get("deadline_time")),
        "text": f"{run.get('dag_id')} run {run.get('dag_run_id') or run.get('run_id')} is running late",
    }
    request = urllib.request.Request(
        OPS_NOTIFY_URL,
        data=json.dumps(notice).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        response.read()
    print(f"late notice posted to {channel}: {notice['text']}")
    return "posted"
