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
