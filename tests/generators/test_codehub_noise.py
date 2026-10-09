"""Actual callback evidence is distinct from schedule admission."""

import json
import threading

from sregym.conductor.scenarios.database_recovery import NoiseDecision, NoiseEvent, NoisePlan
from sregym.generators.noise.codehub import NoiseExecutor


def decision(identity, *, admitted=True, at=0):
    return NoiseDecision(
        identity,
        NoiseEvent(at, 1, "region-a/tenant-000", "traffic-burst"),
        admitted,
        None if admitted else "noise-disabled",
    )


def test_rejected_schedule_never_runs_callback(tmp_path):
    calls = []
    plan = NoisePlan("recovery-noise-v2", (decision("rejected", admitted=False), decision("actual")))
    executor = NoiseExecutor(
        plan, lambda event, cancel: calls.append(event) or {"requests": 4}, tmp_path / "noise.jsonl"
    )
    executor.run()
    assert len(calls) == 1
    receipts = {row.event_id: row for row in executor.receipts}
    assert receipts["rejected"].status == "rejected"
    assert receipts["actual"].status == "executed"
    assert json.loads(receipts["actual"].measurements_json) == {"requests": 4}
    assert len((tmp_path / "noise.jsonl").read_text().splitlines()) == 2


def test_empty_or_failed_execution_is_not_success(tmp_path):
    executor = NoiseExecutor(
        NoisePlan("recovery-noise-v2", (decision("empty"),)), lambda _event, _cancel: {}, tmp_path / "noise.jsonl"
    )
    executor.run()
    assert executor.receipts[0].status == "failed"
    assert executor.receipts[0].reason == "RuntimeError"


def test_runtime_cap_and_cancellation_are_measured(tmp_path):
    started = threading.Event()

    def execute(_event, cancel):
        started.set()
        cancel.wait(2)
        return {"requests": 1}

    plan = NoisePlan("recovery-noise-v2", (decision("active"), decision("over-cap"), decision("future", at=10)))
    executor = NoiseExecutor(plan, execute, tmp_path / "noise.jsonl", max_concurrent=1)
    executor.start()
    assert started.wait(1)
    executor.stop(timeout=3)
    receipts = {row.event_id: row for row in executor.receipts}
    assert receipts["active"].status == "cancelled"
    assert receipts["over-cap"].status in {"not-executed", "cancelled"}
    assert receipts["future"].status == "cancelled"
    assert all(row.status != "executed" for row in receipts.values())
