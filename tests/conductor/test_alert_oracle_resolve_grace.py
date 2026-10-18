"""AlertOracle waits for a fault's alerts to resolve before failing (SREGym#1116).

After a correct fix, the fault's own alert keeps firing until the rule's look-back
window clears and Prometheus evaluates the rule again. Failing at the first firing
alert therefore rejected correct fixes, and more often the faster the fix was.
"""

from types import SimpleNamespace

import sregym.conductor.oracles.alert_oracle as alert_oracle_module
from sregym.conductor.oracles.alert_oracle import AlertOracle

NAMESPACE = "astronomy-shop"
ALERT = {"state": "firing", "labels": {"namespace": NAMESPACE, "alertname": "HighRequestErrorRate"}}


class _FakeClock:
    """Deterministic clock: ``sleep`` advances ``monotonic`` without waiting."""

    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def _oracle(monkeypatch, firing_per_poll, **kwargs):
    """An AlertOracle whose polls follow ``firing_per_poll`` (the last value repeats)."""
    clock = _FakeClock()
    polls = []

    def query(_namespace):
        index = min(len(polls), len(firing_per_poll) - 1)
        polls.append(clock.now)
        return [ALERT] if firing_per_poll[index] else []

    monkeypatch.setattr(alert_oracle_module.time, "sleep", clock.sleep)
    monkeypatch.setattr(alert_oracle_module.time, "monotonic", clock.monotonic)
    oracle = AlertOracle(problem=SimpleNamespace(namespace=NAMESPACE), **kwargs)
    monkeypatch.setattr(oracle, "_query_firing_alerts", query)
    return oracle, clock, polls


def test_alerts_that_clear_within_grace_pass(monkeypatch):
    # The fault's alert is still draining for the first 9 polls after the fix.
    oracle, clock, polls = _oracle(monkeypatch, [True] * 9 + [False])

    assert oracle.evaluate() == {"success": True}
    # The silence window starts at the first clear poll.
    assert clock.now - polls[9] == 120


def test_alerts_that_persist_fail_after_grace(monkeypatch):
    oracle, clock, polls = _oracle(monkeypatch, [True])

    verdict = oracle.evaluate()

    assert verdict["reason"] == "alerts_still_firing"
    assert verdict["detail"]["alerts"] == ["HighRequestErrorRate"]
    assert 300 <= polls[-1] - polls[0] < 310


def test_refiring_alert_restarts_silence_window(monkeypatch):
    # Silent for 50 s, one more firing poll, then silent: the pass needs a full
    # silence window after the re-fire.
    oracle, clock, polls = _oracle(monkeypatch, [False] * 5 + [True] + [False])

    assert oracle.evaluate()["success"] is True
    assert clock.now - polls[6] == 120


def test_zero_grace_fails_on_the_first_firing_poll(monkeypatch):
    oracle, clock, polls = _oracle(monkeypatch, [True], resolve_grace_seconds=0)

    assert oracle.evaluate()["reason"] == "alerts_still_firing"
    assert len(polls) == 1


def test_evaluation_timeout_covers_the_longest_evaluation(monkeypatch):
    # Alerts fire until just before the grace window ends, then a full silence
    # window follows.
    oracle, clock, polls = _oracle(monkeypatch, [True] * 30 + [False])
    begin = clock.now

    assert oracle.evaluate()["success"] is True
    assert clock.now - begin <= oracle.evaluation_timeout_seconds
