from types import SimpleNamespace

import pytest

from clients.stratus.tools.submit_tool import _require_accepted_submission
from mcp_server import submit_server


@pytest.mark.parametrize(
    ("answer", "stage", "expected_payload"),
    [
        ("root cause", "diagnosis", {"solution": "root cause", "stage": "diagnosis"}),
        ("", "mitigation", {"solution": "", "stage": "mitigation"}),
        ("", None, {"solution": ""}),
    ],
)
def test_submission_mcp_forwards_the_stage(monkeypatch, answer, stage, expected_payload):
    request = {}

    def post(url, *, json, headers, timeout):
        request.update(url=url, json=json, headers=headers, timeout=timeout)
        return SimpleNamespace(status_code=200, text='{"status":"200"}')

    monkeypatch.setattr(submit_server.requests, "post", post)

    response = submit_server.submit.fn(answer, stage=stage)

    assert response["status"] == "200"
    assert request["json"] == expected_payload
    assert request["timeout"] == 310


def test_stratus_manual_submission_rejects_an_mcp_error():
    with pytest.raises(RuntimeError, match="rejected the mitigation submission"):
        _require_accepted_submission(
            {"status": "error", "text": "evaluation in progress"},
            "mitigation",
        )


@pytest.mark.parametrize("status", ["200", "done"])
def test_stratus_manual_submission_accepts_success_statuses(status):
    _require_accepted_submission({"status": status}, "mitigation")
