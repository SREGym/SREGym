"""Cluster-free checks of the n8n payment batch oracle, its workload, the portal and the published workflows."""

import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

from sregym.conductor.oracles.apps.n8n.payment_batch import N8nPaymentBatchOracle
from sregym.conductor.problems.apps.n8n.payment_batch_remittance_files_lost import (
    N8nPaymentBatchRemittanceFilesLost,
)
from sregym.service.apps.n8n.payment_batch import N8nPaymentBatch
from sregym.service.apps.n8n.payment_batch_traffic import PaymentBatchTraffic

N8N = Path(__file__).resolve().parents[2] / "sregym" / "service" / "apps" / "n8n"


def workflow(name):
    return json.loads((N8N / "workflows" / name).read_text())


def batch_traffic():
    traffic = PaymentBatchTraffic(endpoints=lambda refresh=False: {}, seed=7)
    batch = traffic.build(2, 1)
    traffic.batches[batch["batch_id"]] = {**batch, "submitted": time.time() - 3600}
    return traffic, batch


def settle(traffic, batch, payouts_per_line=1, advice=True, status="settled"):
    payouts, advices = {}, {}
    for i, line in enumerate(batch["lines"]):
        payouts[line["line_id"]] = [
            {"id": f"PO-{i}{n}", "reference": line["line_id"], "amount": line["amount"], "currency": line["currency"]}
            for n in range(payouts_per_line)
        ]
        if advice:
            text = f"paying {line['currency']} {line['amount']} ({line['line_id']})"
            advices[line["line_id"]] = [{"line_id": line["line_id"], "advice": text}]
    traffic.portal_batches = lambda: {batch["batch_id"]: {"status": status}}
    traffic.payouts = lambda: payouts
    traffic.remittances = lambda: advices


def test_damage_requires_settled_batches_paid_once_and_advised():
    traffic, batch = batch_traffic()
    assert any(line["currency"] != "EUR" for line in batch["lines"])
    oracle = N8nPaymentBatchOracle(problem=None)
    settle(traffic, batch)
    assert traffic.check() == {} and oracle._damage(traffic, [batch["batch_id"]]) is None
    settle(traffic, batch, payouts_per_line=2)
    assert oracle._damage(traffic, [batch["batch_id"]])["reason"] == "duplicate_payouts"
    settle(traffic, batch, payouts_per_line=0, advice=False, status="scheduled")
    assert oracle._damage(traffic, [batch["batch_id"]])["reason"] == "batches_not_completed"
    settle(traffic, batch, advice=False)
    assert oracle._damage(traffic, [batch["batch_id"]])["reason"] == "batches_not_completed"


def test_child_feeds_one_file_node_from_two_branches():
    child = workflow("render_remittance_advices.json")
    trigger = next(n for n in child["nodes"] if n["type"] == "n8n-nodes-base.executeWorkflowTrigger")
    assert trigger["typeVersion"] == 1.2
    assert child["settings"]["saveDataSuccessExecution"] == "none"
    feeds = [
        src
        for src, c in child["connections"].items()
        for out in c["main"]
        for t in out
        if t["node"] == "Advice document"
    ]
    assert sorted(feeds) == ["Cross-border advice", "SEPA advice"]
    parent = workflow("supplier_payment_batch.json")
    hold = next(n for n in parent["nodes"] if n["name"] == "Release hold")
    assert hold["parameters"]["amount"] * 60 >= 65  # parked in the database, not held in memory


def test_parent_reference_to_the_child_is_resolved():
    app = N8nPaymentBatch.__new__(N8nPaymentBatch)
    parent = workflow("supplier_payment_batch.json")
    app.workflow_ids = {}
    try:
        app.resolve_references(parent)
        raise AssertionError("an unresolved reference must fail")
    except RuntimeError:
        pass
    app.workflow_ids = {"Render remittance advices": "abc123"}
    resolved = json.dumps(app.resolve_references(parent))
    assert "@workflow:" not in resolved and '"abc123"' in resolved


def test_submission_puts_a_pruning_tick_inside_the_hold(monkeypatch):
    problem = N8nPaymentBatchRemittanceFilesLost.__new__(N8nPaymentBatchRemittanceFilesLost)
    started = 1_000_000.0
    monkeypatch.setattr(problem, "_main_started", lambda: started)
    for now in (started + 30, started + 1000, started + 2400):
        monkeypatch.setattr(time, "time", lambda now=now: now)
        at = problem._next_submission()
        tick = at + problem.TICK_INTO_HOLD_S
        assert at >= now + 5
        assert (tick - started - 20) % problem.PRUNE_INTERVAL_S == 0
        assert 0 < tick - at < 600


def test_traffic_threads_do_not_start_on_construction():
    before = threading.active_count()
    PaymentBatchTraffic(endpoints=lambda refresh=False: {})
    assert threading.active_count() == before


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _call(base, method, path, body=None):
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(
        base + path, data=data, method=method, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return response.status, json.loads(response.read())


def test_portal_registers_advises_and_settles_a_batch(tmp_path):
    port = _free_port()
    env = {**os.environ, "PORTAL_DB": str(tmp_path / "portal.db"), "PORT": str(port)}
    server = subprocess.Popen(
        [sys.executable, "-I", str(N8N / "runtime" / "payables_portal.py")],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(100):
            try:
                _call(base, "GET", "/healthz")
                break
            except OSError:
                time.sleep(0.1)
        status, row = _call(base, "POST", "/api/payment-batches", {"batch_id": "PB-1", "lines": 1, "cutoff_at": "x"})
        assert status == 201 and row["status"] == "scheduled"
        body = {"batch_id": "PB-1", "line_id": "PB-1-01", "payout_id": "po_1", "amount": "10.00", "advice": "PB-1-01"}
        assert _call(base, "POST", "/api/remittances", body)[0] == 201
        assert _call(base, "POST", "/api/payment-batches/PB-1/settled", {"lines": 1})[1]["status"] == "settled"
        # a resubmission of a settled batch does not reopen it
        assert _call(base, "POST", "/api/payment-batches", {"batch_id": "PB-1", "lines": 1})[1]["status"] == "settled"
        batch = _call(base, "GET", "/api/payment-batches/PB-1")[1]
        assert [r["line_id"] for r in batch["remittances"]] == ["PB-1-01"]
        assert [h["event"] for h in batch["history"]] == ["scheduled", "settled", "scheduled"]
    finally:
        server.terminate()
        server.wait(timeout=10)


def test_problem_is_registered(monkeypatch):
    import sregym.conductor.problems.registry as registry_module

    monkeypatch.setattr(registry_module, "KubeCtl", lambda: None)
    registry = registry_module.ProblemRegistry()
    assert registry.get_problem("n8n_payment_batch_remittance_files_lost") is N8nPaymentBatchRemittanceFilesLost
