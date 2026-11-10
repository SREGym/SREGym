"""Cluster-free checks of the n8n invoice payment oracle, its workload and the published workflow."""

import json
import threading
import time
from pathlib import Path

from sregym.conductor.oracles.apps.n8n.diagnosis import MechanismGatedJudgeOracle
from sregym.conductor.oracles.apps.n8n.invoice_payment import N8nInvoicePaymentOracle
from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.problems.apps.n8n.invoice_payment_incident import N8nInvoicePaymentIncident
from sregym.service.apps.n8n.invoice_payment_traffic import InvoicePaymentTraffic

WORKFLOWS = Path(__file__).resolve().parents[2] / "sregym" / "service" / "apps" / "n8n" / "workflows"


def payout(pid, reference, status="scheduled"):
    return {"id": pid, "reference": reference, "status": status, "created_at": "2026-11-09T01:00:00.000Z"}


def workflow(name):
    return json.loads((WORKFLOWS / name).read_text())


class FakePayouts:
    def __init__(self, traffic, payouts):
        self.items = payouts
        traffic.payouts = lambda: list(self.items)


class FakeInvoiceApp:
    def __init__(self, portal_rows, executions):
        self.portal_rows = portal_rows
        self.executions = executions

    def portal(self, path):
        return list(self.portal_rows.values())

    def psql(self, sql):
        return [
            [e, r["status"], "t" if r.get("finished") else "f", r.get("retry", "")] for e, r in self.executions.items()
        ]


class FakeProblem:
    def __init__(self, app, affected):
        self.app = app
        self.affected = affected


def invoice_setup():
    traffic = InvoicePaymentTraffic(endpoints=lambda refresh=False: {})
    now = time.time()
    traffic.approved = {i: {"approved_at": now - 3600, "posts": []} for i in ("INV-1", "INV-2", "INV-3")}
    fake = FakePayouts(traffic, [payout("PO-1", "INV-1"), payout("PO-2", "INV-2"), payout("PO-3", "INV-3")])
    portal = {i: {"id": i, "status": "paid", "payout_id": f"PO-{i[-1]}"} for i in traffic.approved}
    executions = {"5": {"status": "crashed"}, "6": {"status": "crashed"}}
    app = FakeInvoiceApp(portal, executions)
    oracle = N8nInvoicePaymentOracle(problem=FakeProblem(app, {"5": {"invoice": "INV-1"}, "6": {"invoice": "INV-2"}}))
    return traffic, fake, app, oracle, now


def test_damage_requires_one_live_payout_that_the_portal_points_at():
    traffic, fake, app, oracle, now = invoice_setup()
    assert oracle._damage(app, traffic, now) is None
    fake.items.append(payout("PO-4", "INV-1"))
    assert oracle._damage(app, traffic, now)["reason"] == "duplicate_payouts_remain"
    fake.items[3]["status"] = "cancelled"
    assert oracle._damage(app, traffic, now) is None
    fake.items[0]["status"] = "cancelled"
    assert oracle._damage(app, traffic, now)["reason"] == "invoice_left_unpaid"
    fake.items[0]["status"] = "scheduled"
    app.portal_rows["INV-3"]["status"] = "approved"
    assert oracle._damage(app, traffic, now)["reason"] == "portal_payout_mismatch"


def test_damage_fails_when_a_handover_payout_was_deleted():
    traffic, fake, app, oracle, now = invoice_setup()
    oracle.handover_payouts = {"PO-1", "PO-2", "PO-3"}
    del fake.items[2]
    assert oracle._damage(app, traffic, now)["reason"] == "payouts_deleted"


def test_records_must_show_the_completed_runs_as_succeeded():
    traffic, fake, app, oracle, now = invoice_setup()
    assert oracle._records(app)[0]["reason"] == "completed_runs_still_recorded_unfinished"
    app.executions["5"] = {"status": "success", "finished": True}
    app.executions["6"] = {"status": "crashed", "retry": "9"}
    assert oracle._records(app)[0] is None
    del app.executions["6"]
    assert oracle._records(app)[0]["reason"] == "executions_deleted"


def test_oracle_without_affected_runs_checks_no_records():
    traffic, fake, app, oracle, now = invoice_setup()
    oracle.problem.affected = {}
    assert oracle._records(app) == (None, {})


def test_workflow_pays_before_its_short_wait_and_marks_paid_last():
    wf = workflow("supplier_invoice_payment.json")
    names = [n["name"] for n in wf["nodes"]]
    assert names == ["Approved invoice", "Schedule payout", "Bank confirmation", "Payout status", "Mark invoice paid"]
    wait = next(n for n in wf["nodes"] if n["name"] == "Bank confirmation")
    # n8n keeps waits shorter than 65 s in the worker's process
    assert wait["parameters"]["unit"] == "seconds" and wait["parameters"]["amount"] < 65
    for source, outputs in wf["connections"].items():
        assert source in names
        for branch in outputs["main"]:
            for target in branch:
                assert target["node"] in names


def test_traffic_threads_do_not_start_on_construction():
    before = threading.active_count()
    InvoicePaymentTraffic(endpoints=lambda refresh=False: {})
    assert threading.active_count() == before


def test_unpaid_skips_invoices_with_a_live_payout_or_a_pending_post():
    traffic, fake, app, oracle, now = invoice_setup()
    traffic.approved["INV-4"] = {"approved_at": now - 3600, "posts": []}
    traffic.approved["INV-5"] = {"approved_at": now - 3600, "posts": []}
    traffic._outbox = [(now + 30, "INV-5")]
    assert traffic.unpaid(older_than_s=600) == ["INV-4"]


def _judge_result(d2q1, d2q3, success=True):
    return {
        "success": success,
        "judgment": str(success),
        "checklist": [{"id": "D2-Q1", "answer": d2q1}, {"id": "D2-Q3", "answer": d2q3}],
    }


def test_gated_judge_fails_a_passing_composite_without_the_mechanism(monkeypatch):
    oracle = MechanismGatedJudgeOracle.__new__(MechanismGatedJudgeOracle)
    monkeypatch.setattr(LLMAsAJudgeOracle, "evaluate", lambda self, s, d=None: _judge_result("Yes", "No"))
    result = oracle.evaluate("the database crash interrupted the runs")
    assert result["success"] is False
    assert result["gated_by"] == ["D2-Q3"]

    monkeypatch.setattr(LLMAsAJudgeOracle, "evaluate", lambda self, s, d=None: _judge_result("Yes", "Yes"))
    result = oracle.evaluate("the runs paid, but the worker's final save failed and was never retried")
    assert result["success"] is True
    assert "gated_by" not in result

    monkeypatch.setattr(LLMAsAJudgeOracle, "evaluate", lambda self, s, d=None: {"success": None})
    assert MechanismGatedJudgeOracle.evaluate(oracle, "x")["success"] is None


def test_problem_is_registered(monkeypatch):
    import sregym.conductor.problems.registry as registry_module

    monkeypatch.setattr(registry_module, "KubeCtl", lambda: None)
    registry = registry_module.ProblemRegistry()
    assert registry.get_problem("n8n_invoice_payment_incident") is N8nInvoicePaymentIncident
