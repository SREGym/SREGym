import pytest

from clients.claudecode.driver import build_instruction


def test_mitigation_start_does_not_request_a_premature_diagnosis_submission():
    prompt = build_instruction({"app_name": "GitLab", "namespace": "gitlab-ce"}, stage="mitigation")
    assert "TASK 1: DIAGNOSIS" not in prompt
    assert "For DIAGNOSIS stage" not in prompt
    assert "Submit only after implementing and checking the fix" in prompt


def test_diagnosis_start_preserves_the_two_stage_workflow():
    prompt = build_instruction({}, stage="diagnosis")
    assert "TASK 1: DIAGNOSIS" in prompt
    assert "TASK 2: MITIGATION" in prompt
    assert "For DIAGNOSIS stage" in prompt


def test_unknown_stage_is_rejected():
    with pytest.raises(ValueError, match="Unsupported ready stage"):
        build_instruction({}, stage="finished")


def test_multiple_namespaces_are_still_listed_in_a_mitigation_prompt():
    """The namespace block is built before the stage branch; keep it in both."""
    prompt = build_instruction({"namespaces": ["gitlab-ce", "observability"]}, stage="mitigation")
    assert "Namespaces: gitlab-ce, observability" in prompt
    assert "investigate all of them" in prompt
