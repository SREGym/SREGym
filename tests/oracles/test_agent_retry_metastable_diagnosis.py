"""Tests for AgentRetryMetastableDiagnosisOracle."""

from types import SimpleNamespace

from sregym.conductor.oracles.agent_retry_metastable_diagnosis import AgentRetryMetastableDiagnosisOracle


def test_strong_diagnosis_passes():
    problem = SimpleNamespace()
    oracle = AgentRetryMetastableDiagnosisOracle(problem=problem)

    strong_diagnosis = (
        "A transient backend latency spike triggered retries independently at the client/tool and "
        "agent-planner layers. The compounded attempts saturated backend concurrency/connection capacity; "
        "queued and retried work continued after the original latency fault was removed, sustaining the outage."
    )

    result = oracle.evaluate(strong_diagnosis)
    assert result.get("success") is True
    assert result.get("accuracy") >= 75.0
    assert result["dimensions"]["localization"]["score"] >= 0.75
    assert result["dimensions"]["trigger"]["score"] >= 0.75
    assert result["dimensions"]["sustaining_mechanism"]["score"] >= 0.75
    assert result["dimensions"]["impact"]["score"] >= 0.75


def test_weak_diagnosis_overloaded_fails():
    problem = SimpleNamespace()
    oracle = AgentRetryMetastableDiagnosisOracle(problem=problem)

    weak_diagnosis = "PostgreSQL is overloaded."
    result = oracle.evaluate(weak_diagnosis)

    assert result.get("success") is False
    assert result.get("accuracy") < 50.0


def test_weak_diagnosis_latency_increased_fails():
    problem = SimpleNamespace()
    oracle = AgentRetryMetastableDiagnosisOracle(problem=problem)

    weak_diagnosis = "Backend latency increased."
    result = oracle.evaluate(weak_diagnosis)

    assert result.get("success") is False
    assert result.get("accuracy") < 50.0


def test_missing_sustaining_mechanism_fails():
    problem = SimpleNamespace()
    oracle = AgentRetryMetastableDiagnosisOracle(problem=problem)

    incomplete_diagnosis = (
        "A transient latency spike in the backend caused slow responses in the agent-to-backend request path, "
        "saturating the worker concurrency pool."
    )
    result = oracle.evaluate(incomplete_diagnosis)

    assert result.get("success") is False
    assert result.get("reason") == "missing_sustaining_mechanism"
