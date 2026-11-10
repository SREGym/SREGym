"""Pure-logic checks of the pretix waiting-list mitigation oracle (no cluster)."""

from types import SimpleNamespace

from sregym.conductor.oracles.apps.pretix.waitinglist_served import PretixWaitingListServedOracle, _in_order


def _quota(**kw):
    base = {"closed": False, "free_unoffered": 0, "unserved": 0}
    base.update(kw)
    return base


def test_starved_only_closed_quotas_with_free_unoffered_seats_and_waiting_people():
    oracle = PretixWaitingListServedOracle(SimpleNamespace())
    state = {
        "quotas": {
            "1": _quota(closed=True, free_unoffered=2, unserved=3),  # the incident
            "2": _quota(closed=True, free_unoffered=0, unserved=4),  # sold out for real
            "3": _quota(closed=True, free_unoffered=2, unserved=0),  # nobody waiting
            "4": _quota(closed=False, free_unoffered=2, unserved=3),  # open, the cron will serve
        }
    }
    assert oracle.starved(state) == ["1"]


def test_in_order():
    e = lambda i, v: {"priority": 0, "created": f"2026-11-09T10:0{i}:00", "voucher": v}  # noqa: E731
    assert _in_order([e(1, 7), e(2, 8), e(3, None)])
    assert not _in_order([e(1, None), e(2, 8)])


def test_waitinglist_closed_after_handover_is_reported_not_failed(monkeypatch):
    import sregym.conductor.oracles.apps.pretix.waitinglist_served as W

    now = 2_000_000_000.0
    monkeypatch.setattr(W.time, "time", lambda: now)
    quotas = {
        "1": {"closed": True, "free_unoffered": 0, "unserved": 0, "first_closed_at": now - 900, "size": 14},
        "2": {"closed": True, "free_unoffered": 1, "unserved": 2, "first_closed_at": now - 120, "size": 12},
        "3": {"closed": True, "free_unoffered": 1, "unserved": 2, "first_closed_at": now - 600, "size": 12},
    }
    for q in quotas.values():
        q.update(
            {
                "name": "w",
                "close_when_sold_out": True,
                "sold": 10,
                "offered": 0,
                "entries": [],
                "registered_emails": [],
                "offers": [],
            }
        )
    state = {"event": {"live": True, "waiting_list_enabled": True, "waiting_list_auto": True}, "quotas": quotas}
    problem = SimpleNamespace(
        target={"quota": 1, "item": 1}, handover={"entries": [], "emails": {}}, handover_time=now - 300
    )
    oracle = W.PretixWaitingListServedOracle(problem)
    oracle.baseline = {"volumes": {}, "sizes": {k: v["size"] for k, v in quotas.items()}}
    monkeypatch.setattr(oracle, "_platform", lambda: [])
    monkeypatch.setattr(oracle, "_probe", lambda: None)
    monkeypatch.setattr(oracle, "state", lambda: state)
    monkeypatch.setattr(oracle, "_redeem", lambda st: None)
    verdict = oracle.evaluate()
    assert verdict["reason"] == "workshop_closed_with_free_seats"
    assert list(verdict["detail"]["quotas"]) == ["3"]
    assert verdict["report"]["closed_after_handover"] == ["2"]
