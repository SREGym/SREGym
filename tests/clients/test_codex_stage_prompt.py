from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from clients.codex.driver import build_instruction
from clients.jev.config import MODEL_ENV


def test_mitigation_start_does_not_request_a_premature_diagnosis_submission(monkeypatch):
    monkeypatch.delenv(MODEL_ENV, raising=False)
    prompt = build_instruction({"app_name": "Gitea", "namespace": "gitea"}, stage="mitigation")
    assert "TASK 1: DIAGNOSIS" not in prompt
    assert "For DIAGNOSIS stage" not in prompt
    assert '"stage": "mitigation", "solution": ""' in prompt
    assert "Submit only after implementing and checking the fix" in prompt


def test_diagnosis_start_preserves_the_two_stage_workflow(monkeypatch):
    monkeypatch.delenv(MODEL_ENV, raising=False)
    prompt = build_instruction({}, stage="diagnosis")
    assert "TASK 1: DIAGNOSIS" in prompt
    assert "TASK 2: MITIGATION" in prompt


def test_unknown_stage_is_rejected():
    with pytest.raises(ValueError, match="Unsupported ready stage"):
        build_instruction({}, stage="finished")


def test_main_passes_explicit_agent_image_to_the_benchmark(monkeypatch):
    import main

    monkeypatch.setattr(main, "init_logger", lambda: None)
    run = Mock(return_value="done")
    monkeypatch.setattr(main, "_run_benchmark", run)
    args = SimpleNamespace(
        judge_backend="api", use_external_harness=False, force_build=False, agent_image="local-driver:test"
    )
    assert main.main(args) == "done"
    assert run.call_args.kwargs["agent_image"] == "local-driver:test"
