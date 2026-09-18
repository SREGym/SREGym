"""Failure classification for the oracles that many problems share.

These three carry most of the leverage in the classification work, because they
are not per-problem code: ``AlertOracle`` backs 23 problems, ``WorkloadOracle``
5, and both are usually reached through ``CompoundedOracle``. A bare
``{"success": False}`` here was miscounting failures across a large fraction of
the suite at once.
"""

import pytest

from sregym.conductor.oracles.alert_oracle import AlertOracle
from sregym.conductor.oracles.failure import FailureClass
from sregym.conductor.oracles.sustained_readiness import SustainedReadinessOracle
from sregym.conductor.oracles.workload import WorkloadOracle


class _Problem:
    namespace = "hotel-reservation"


# ----------------------------------------------------------------------------
# AlertOracle
# ----------------------------------------------------------------------------


@pytest.fixture
def alert_oracle():
    """An AlertOracle with no buffer wait and a single poll."""
    oracle = AlertOracle.__new__(AlertOracle)
    oracle.problem = _Problem()
    oracle.buffer_seconds = 0
    oracle.sustained_silence_seconds = 1
    oracle.poll_interval_seconds = 0
    oracle.exclude_alerts = set()
    oracle._baseline_instances = set()
    return oracle


def test_unreachable_prometheus_is_environmental_not_a_harness_error(alert_oracle, monkeypatch, capsys):
    """The instrument failing is the cluster's problem, not ours and not the model's.

    ``_query_firing_alerts`` raises ``RuntimeError`` when it cannot reach
    Prometheus. Left to propagate it reaches the conductor's exception handler
    and is recorded as ``harness_error`` -- blaming SREGym's code for an
    infrastructure failure, across all 23 problems that use this oracle.
    """

    def unreachable(_namespace):
        raise RuntimeError("Failed to query Prometheus alerts")

    monkeypatch.setattr(alert_oracle, "_query_firing_alerts", unreachable)

    verdict = alert_oracle.evaluate()

    assert verdict["success"] is False
    assert verdict["reason"] == "prometheus_unreachable"
    assert verdict["failure_class"] == FailureClass.ENVIRONMENT_ERROR
    assert "Cannot reach Prometheus" in capsys.readouterr().out


def test_a_still_firing_alert_is_the_agents_failure(alert_oracle, monkeypatch, capsys):
    """Decisive rather than ambiguous, because baseline filtering already ran."""
    alert = {"labels": {"alertname": "HighLatency", "namespace": "hotel-reservation", "severity": "critical"}}
    monkeypatch.setattr(alert_oracle, "_query_firing_alerts", lambda _ns: [alert])

    verdict = alert_oracle.evaluate()

    assert verdict["reason"] == "alerts_still_firing"
    assert verdict["failure_class"] == FailureClass.AGENT_ERROR
    assert verdict["detail"]["alerts"] == ["HighLatency"]
    # The print names the full instance; the detail carries only the names.
    assert "HighLatency" in capsys.readouterr().out


def test_firing_alert_names_are_deduplicated_and_ordered(alert_oracle, monkeypatch):
    """``detail`` should be stable enough to group on across attempts."""
    alerts = [
        {"labels": {"alertname": "HighLatency", "pod": "frontend-1"}},
        {"labels": {"alertname": "HighLatency", "pod": "frontend-2"}},
        {"labels": {"alertname": "CrashLooping", "pod": "search-1"}},
    ]
    monkeypatch.setattr(alert_oracle, "_query_firing_alerts", lambda _ns: alerts)

    assert alert_oracle.evaluate()["detail"]["alerts"] == ["CrashLooping", "HighLatency"]


def test_silence_still_passes_without_failure_columns(alert_oracle, monkeypatch):
    monkeypatch.setattr(alert_oracle, "_query_firing_alerts", lambda _ns: [])

    verdict = alert_oracle.evaluate()

    assert verdict == {"success": True}


# ----------------------------------------------------------------------------
# WorkloadOracle
# ----------------------------------------------------------------------------


class _Entry:
    def __init__(self, ok, log=""):
        self.ok = ok
        self.log = log
        self.time = "2026-09-07T12:00:00Z"


class _Wrk:
    def __init__(self, entries=None, raises=None):
        self._entries = entries or []
        self._raises = raises

    def collect(self, number=1):
        if self._raises is not None:
            raise self._raises
        return self._entries[:number]


def _workload(wrk):
    oracle = WorkloadOracle.__new__(WorkloadOracle)
    oracle.problem = _Problem()
    oracle.wrk = wrk
    return oracle


def test_failing_requests_are_ambiguous_not_the_agents_fault(capsys):
    """No pre-fault baseline means this cannot be attributed.

    A request failing may be the agent leaving the fault in place or unrelated
    cluster degradation. With ``importance`` 3.0 this oracle dominates compound
    accuracy, so guessing "agent" here would be the loudest possible way to be
    wrong.
    """
    oracle = _workload(_Wrk(entries=[_Entry(ok=False, log="connection refused")] * 50))

    verdict = oracle.evaluate()

    assert verdict["reason"] == "workload_requests_failing"
    assert verdict["failure_class"] == FailureClass.AMBIGUOUS
    assert "connection refused" in verdict["detail"]["log"]
    assert "failed with log" in capsys.readouterr().out


def test_a_collection_error_is_reported_rather_than_raised(capsys):
    """The load generator not reporting is not evidence about the agent."""
    oracle = _workload(_Wrk(raises=RuntimeError("wrk pod evicted")))

    verdict = oracle.evaluate()

    assert verdict["reason"] == "workload_collection_failed"
    assert verdict["failure_class"] == FailureClass.AMBIGUOUS
    assert "RuntimeError: wrk pod evicted" in verdict["detail"]["error"]
    assert "Error during workload collection" in capsys.readouterr().out


def test_healthy_workload_passes_unchanged():
    oracle = _workload(_Wrk(entries=[_Entry(ok=True)] * 50))
    assert oracle.evaluate() == {"success": True}


# ----------------------------------------------------------------------------
# SustainedReadinessOracle
# ----------------------------------------------------------------------------


def _readiness(ready_sequence):
    """An oracle whose readiness check yields *ready_sequence* in order."""
    oracle = SustainedReadinessOracle.__new__(SustainedReadinessOracle)
    oracle.problem = _Problem()
    oracle.problem.kubectl = object()
    oracle.buffer_period = 1
    oracle.sustained_period = 1
    oracle.check_interval = 0
    results = iter(ready_sequence)
    oracle._check_all_pods_ready = lambda *a, **k: next(results, False)
    return oracle


def test_pods_that_never_become_ready_use_the_shared_reason(capsys):
    oracle = _readiness([False] * 500)

    verdict = oracle.evaluate()

    assert verdict["reason"] == "pods_not_ready"
    assert verdict["failure_class"] == FailureClass.AMBIGUOUS
    assert "did not become ready" in capsys.readouterr().out


def test_readiness_that_regresses_gets_its_own_reason(capsys):
    """Distinct from ``pods_not_ready``: the mitigation worked, then did not.

    Both are undecidable, but "unstable fix" and "no fix" are different
    findings and collapsing them loses the more interesting one.
    """
    oracle = _readiness([True, False])

    verdict = oracle.evaluate()

    assert verdict["reason"] == "readiness_not_sustained"
    assert verdict["failure_class"] == FailureClass.AMBIGUOUS
    assert verdict["detail"]["required_seconds"] == 1
    assert "readiness check failed" in capsys.readouterr().out
