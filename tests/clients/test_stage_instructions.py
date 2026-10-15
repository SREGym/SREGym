"""Stage-specific workflows must preserve context and the submission method."""

import pytest

from clients.claudecode.driver import build_instruction as claude_instruction
from clients.codex.driver import build_instruction as codex_instruction
from clients.jev.config import INSTRUCTION, MODEL_ENV


@pytest.mark.parametrize("build", [codex_instruction, claude_instruction])
def test_default_workflow_remains_two_stage(build, monkeypatch):
    monkeypatch.delenv(MODEL_ENV, raising=False)
    prompt = build({"app_name": "test", "namespace": "test"})
    assert "TASK 1: DIAGNOSIS" in prompt
    assert "TASK 2: MITIGATION" in prompt
    assert 'with JSON: {"solution": ""}' in prompt
    assert "jev_" not in prompt


@pytest.mark.parametrize("build", [codex_instruction, claude_instruction])
def test_mitigation_only_workflow_preserves_application_description(build, monkeypatch):
    monkeypatch.delenv(MODEL_ENV, raising=False)
    description = "WORKFLOW: You will perform TWO tasks in sequence:\napplication-owned text\nImportant: keep this"
    prompt = build({"app_name": "test", "namespace": "test", "descriptions": description, "stages": ["mitigation"]})
    assert description in prompt
    assert "TASK 1: DIAGNOSIS" not in prompt
    assert "TASK: MITIGATION" in prompt
    assert 'with JSON: {"solution": ""}' in prompt


def test_mitigation_only_jev_has_no_diagnosis_or_http_submission(monkeypatch):
    monkeypatch.setenv(MODEL_ENV, "test-model")
    prompt = codex_instruction({"app_name": "test", "namespace": "test", "stages": ["mitigation"]})
    assert "stage=mitigation" in prompt
    assert "Use `jev_submit` for diagnosis" not in prompt
    assert "For diagnosis, set stage=diagnosis" not in prompt
    assert "POST " not in prompt
    assert "Do not bypass the review with curl" in prompt


def test_normal_jev_workflow_is_unchanged(monkeypatch):
    monkeypatch.setenv(MODEL_ENV, "test-model")
    prompt = codex_instruction({"app_name": "test", "namespace": "test", "stages": ["diagnosis", "mitigation"]})
    assert prompt.endswith(INSTRUCTION)
    assert "Use `jev_submit` for both stages" in prompt


def test_claude_mitigation_keeps_multiple_namespaces(monkeypatch):
    monkeypatch.delenv(MODEL_ENV, raising=False)
    prompt = claude_instruction({"namespace": "test", "namespaces": ["test", "other"], "stages": ["mitigation"]})
    assert "Namespaces: test, other" in prompt
