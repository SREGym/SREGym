"""Pure-logic checks of the Airflow carrier-pickup mitigation oracle and gated judge (no cluster)."""

import json
from types import SimpleNamespace

from sregym.conductor.oracles.apps.airflow import carrier_pickups as C
from sregym.conductor.oracles.apps.airflow.carrier_pickups import AirflowCarrierPickupOracle
from sregym.conductor.oracles.apps.airflow.diagnosis import MechanismGatedJudgeOracle
from sregym.conductor.oracles.apps.airflow.shop_orders import restarted_since
from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle

RUN = "scheduled__2026-11-10T13:53:00+00:00"


class FakeApp:
    """Answers the oracle's psql/kubectl calls from an in-memory shop state."""

    def __init__(self, notifications, lines=10, parcels=10, unrequested=(), paused="f"):
        self.notifications = notifications  # [(manifest_id, payload dict)]
        self.lines = lines
        self.parcels = parcels
        self.unrequested = list(unrequested)
        self.paused = paused

    def shop_psql(self, sql, timeout=60):
        if "GROUP BY" in sql:
            counts = {}
            for manifest, _ in self.notifications:
                counts[manifest] = counts.get(manifest, 0) + 1
            return "\n".join(f"{m}|{n}" for m, n in sorted(counts.items()) if n > 1)
        if sql.startswith("SELECT count(*) FROM ops_notifications"):
            manifest = sql.split("manifest_id'='")[1].split("'")[0]
            return str(sum(1 for m, _ in self.notifications if m == manifest))
        if sql.startswith("SELECT payload FROM ops_notifications"):
            manifest = sql.split("manifest_id'='")[1].split("'")[0]
            return "\n".join(json.dumps(p) for m, p in self.notifications if m == manifest)
        if "FROM manifests m WHERE m.manifest_id" in sql:
            return f"{self.parcels}|{self.lines}|t"
        if "NOT EXISTS" in sql:
            return "\n".join(self.unrequested)
        if "FROM manifests WHERE dag_run_id" in sql:
            return "1"
        raise AssertionError(sql)

    def psql(self, sql, timeout=60):
        if "is_paused" in sql:
            return self.paused
        if "try_number > 1" in sql:
            return "0"
        raise AssertionError(sql)

    def command(self, *args, **kwargs):
        if args[:2] == ("get", "pods"):
            return json.dumps({"items": []})
        if args[0] == "get":
            return "1"
        raise AssertionError(args)


def _oracle(app, monkeypatch):
    problem = SimpleNamespace(
        app=app, incident_manifest="4104", incident_parcels=10, incident_run_id=RUN, handover_snapshot=None
    )
    oracle = AirflowCarrierPickupOracle(problem)
    monkeypatch.setattr(oracle, "WAIT_S", 0)
    monkeypatch.setattr(C.time, "sleep", lambda s: None)
    monkeypatch.setattr(oracle, "_probe", lambda: {"ok": True})
    return oracle


def _callback_payload(manifest="4104", run_id=RUN):
    return {"manifest_id": int(manifest), "run_id": run_id, "dag_id": "carrier_manifest", "parcels": 10}


def test_lost_pickup_fails(monkeypatch):
    oracle = _oracle(FakeApp([("4103", _callback_payload("4103"))], unrequested=["4104"]), monkeypatch)
    verdict = oracle.evaluate()
    assert verdict["success"] is False
    assert verdict["reason"] == "pickup_not_requested"
    assert verdict["checks"]["all_manifests_requested"]["manifests_without_pickup"] == ["4104"]


def test_pickup_requested_once_by_the_callback_code_passes(monkeypatch):
    oracle = _oracle(FakeApp([("4103", _callback_payload("4103")), ("4104", _callback_payload())]), monkeypatch)
    verdict = oracle.evaluate()
    assert verdict["success"] is True
    assert verdict["collateral"]["incident_pickup_posted_by_hand"] is False
    assert verdict["collateral"]["incident_run_manifests"] == "1"


def test_pickup_posted_by_hand_passes_and_is_reported(monkeypatch):
    by_hand = {"manifest_id": 4104, "channel": "carrier-pickups", "text": "pickup requested (on-call)"}
    oracle = _oracle(FakeApp([("4104", by_hand)]), monkeypatch)
    verdict = oracle.evaluate()
    assert verdict["success"] is True
    assert verdict["collateral"]["incident_pickup_posted_by_hand"] is True


def test_pickup_requested_twice_fails(monkeypatch):
    oracle = _oracle(FakeApp([("4104", _callback_payload()), ("4104", _callback_payload())]), monkeypatch)
    verdict = oracle.evaluate()
    assert verdict["success"] is False
    assert verdict["reason"] == "pickup_requested_twice"


def test_emptied_manifest_fails(monkeypatch):
    oracle = _oracle(FakeApp([("4104", _callback_payload())], lines=0), monkeypatch)
    verdict = oracle.evaluate()
    assert verdict["reason"] == "manifest_altered"


def test_paused_dag_fails_first(monkeypatch):
    oracle = _oracle(FakeApp([("4104", _callback_payload())], paused="t"), monkeypatch)
    verdict = oracle.evaluate()
    assert verdict["reason"] == "carrier_handoff_disabled"


def test_restarted_since_reports_new_pods_and_restarts(monkeypatch):
    import sregym.conductor.oracles.apps.airflow.shop_orders as S

    before = {"scheduler": {"a": 0}, "worker": {"w": 0}, "postgres": {"p": 1}}
    now = {"scheduler": {"b": 0}, "worker": {"w": 1}, "postgres": {"p": 1}}
    monkeypatch.setattr(S, "pod_snapshot", lambda app: now)
    assert restarted_since(None, before) == ["scheduler", "worker"]
    assert restarted_since(None, None) == []


def _judge_result(d2q1, d2q3, success=True):
    return {
        "success": success,
        "judgment": str(success),
        "checklist": [{"id": "D2-Q1", "answer": d2q1}, {"id": "D2-Q3", "answer": d2q3}],
    }


def test_gated_judge_fails_a_passing_composite_without_the_mechanism(monkeypatch):
    oracle = MechanismGatedJudgeOracle.__new__(MechanismGatedJudgeOracle)
    monkeypatch.setattr(LLMAsAJudgeOracle, "evaluate", lambda self, s, d=None: _judge_result("No", "Yes"))
    result = oracle.evaluate("callbacks are best-effort")
    assert result["success"] is False
    assert result["gated_by"] == ["D2-Q1"]

    monkeypatch.setattr(LLMAsAJudgeOracle, "evaluate", lambda self, s, d=None: _judge_result("Yes", "Yes"))
    result = oracle.evaluate("scheduler committed success, then died before writing the callback")
    assert result["success"] is True
    assert "gated_by" not in result

    monkeypatch.setattr(LLMAsAJudgeOracle, "evaluate", lambda self, s, d=None: {"success": None})
    assert MechanismGatedJudgeOracle.evaluate(oracle, "x")["success"] is None
