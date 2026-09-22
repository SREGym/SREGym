"""Tests for the CloudThinker driver's session artifact."""

import importlib
import json
import sys

import pytest


@pytest.fixture
def driver(monkeypatch):
    """Import the driver with the two variables it refuses to start without."""
    monkeypatch.setenv("CT_PROMPT_MODE", "scoped")
    monkeypatch.setenv("CT_LANE", "chat")
    sys.modules.pop("clients.cloudthinker.driver", None)
    return importlib.import_module("clients.cloudthinker.driver")


def _read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_write_session_reads_a_conversation_once(driver, tmp_path, monkeypatch):
    calls = []

    def fake_read(conversation_id):
        calls.append(conversation_id)
        return [{"message_id": "m1", "role": "user", "message_content": "hello"}]

    monkeypatch.setattr(driver, "read_transcript", fake_read)
    driver.write_session(
        tmp_path,
        "service_port_conflict_hotel_reservation",
        [
            {"name": "diagnosis", "conversation_id": "conv-1", "submitted": "diag", "rows_at_submission": 1},
            {"name": "mitigation", "conversation_id": "conv-1", "submitted": ""},
        ],
    )

    session = _read(tmp_path / "cloudthinker_session.json")
    # The mitigation turn resumes the diagnosis conversation; reading it twice
    # would put the diagnosis in the trajectory a second time.
    assert calls == ["conv-1"]
    assert session["schema"] == "cloudthinker_session/v1"
    assert [stage["name"] for stage in session["stages"]] == ["diagnosis", "mitigation"]
    assert session["stages"][0]["rows_at_submission"] == 1
    assert [entry["conversation_id"] for entry in session["conversations"]] == ["conv-1"]
    assert len(session["conversations"][0]["records"]) == 1


def test_write_session_survives_an_unreadable_transcript(driver, tmp_path, monkeypatch):
    monkeypatch.setattr(driver, "read_transcript", lambda conversation_id: None)
    driver.write_session(
        tmp_path,
        "problem",
        [{"name": "diagnosis", "conversation_id": "conv-1", "submitted": "diag"}],
    )

    session = _read(tmp_path / "cloudthinker_session.json")
    assert session["conversations"][0]["records"] == []
    # A watermark nobody could read stays absent rather than becoming zero, which
    # the adapter would read as "the diagnosis said nothing".
    assert session["stages"][0]["rows_at_submission"] is None


def test_transcript_sql_reads_the_component_tables_of_one_conversation(driver):
    sql = driver._TRANSCRIPT_SQL.format(conversation_id="conv-1")

    assert "conv-1" in sql
    for table in ("messagecomponent", "messagetextcomponent", "messagethinkingcomponent", "messagetoolcomponent"):
        assert table in sql


def test_every_prompt_mode_has_a_diagnosis_and_a_mitigation_prompt(driver):
    assert set(driver.PROMPT_MODES) == {"guided", "neutral", "scoped", "scoped_rca", "scoped_rca_corpus"}
    for mode, (diagnosis, mitigation) in driver.PROMPT_MODES.items():
        assert diagnosis.strip(), mode
        assert mitigation.strip(), mode


def test_run_turn_returns_resume_failure(driver, monkeypatch):
    conversation_id = "00000000-0000-0000-0000-000000000001"
    responses = iter(
        [
            {"status": "complete", "conversation_id": conversation_id, "answer": "partial"},
            {"status": "error", "conversation_id": conversation_id, "error": "resume failed"},
        ]
    )
    monkeypatch.setattr(driver, "CT_GATE_SETTLE_SECONDS", 0)
    monkeypatch.setattr(driver, "CT_RESUME_RETRIES", 0)
    monkeypatch.setattr(driver, "ct_chat", lambda *args, **kwargs: next(responses))
    monkeypatch.setattr(driver, "pending_approval", lambda value: True)

    result = driver.run_turn("diagnose")

    assert result["error"] == "resume failed"
    assert result["answer"] == "partial"


def test_run_turn_fails_when_approval_budget_is_exhausted(driver, monkeypatch):
    conversation_id = "00000000-0000-0000-0000-000000000001"
    monkeypatch.setattr(driver, "CT_MAX_APPROVALS", 0)
    monkeypatch.setattr(
        driver,
        "ct_chat",
        lambda *args, **kwargs: {
            "status": "complete",
            "conversation_id": conversation_id,
            "answer": "partial",
        },
    )
    monkeypatch.setattr(driver, "pending_approval", lambda value: True)

    result = driver.run_turn("diagnose")

    assert result["status"] == "error"
    assert result["error"] == "approval_limit_exceeded"


def test_prompt_params_include_every_namespace(driver):
    params = driver.prompt_params(
        {
            "app_name": "multi-app",
            "namespace": "first",
            "namespaces": ["first", "second"],
            "descriptions": "two failures",
        }
    )

    assert params["namespace_block"] == (
        "Namespaces: first, second\n(This scenario spans multiple namespaces; investigate all of them.)"
    )


def test_mitigation_harness_failure_is_invalidated(driver, tmp_path, monkeypatch):
    submissions = []
    saved = []
    sessions = []
    monkeypatch.setattr(driver, "submit_to_conductor", submissions.append)
    monkeypatch.setattr(driver, "_save_stage_result", lambda logs, stage, summary: saved.append(stage))
    monkeypatch.setattr(driver, "write_session", lambda logs, problem, stages: sessions.extend(stages))
    monkeypatch.setattr(driver, "_finish", lambda logs, problem: None)

    with pytest.raises(SystemExit) as exc:
        driver.abort_harness_failure(
            tmp_path,
            "problem",
            {"harness_failure": True, "error": "resume failed", "conversation_id": "conv-1"},
            reason="mitigation never reached the agent",
            stage="mitigation",
            stages=[{"name": "diagnosis"}, {"name": "mitigation"}],
        )

    assert exc.value.code == 2
    assert submissions == [""]
    assert saved == ["mitigation"]
    assert [stage["name"] for stage in sessions] == ["diagnosis", "mitigation"]
    assert (tmp_path / "HARNESS_FAILURE.txt").is_file()
