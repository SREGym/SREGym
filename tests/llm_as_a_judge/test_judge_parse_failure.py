"""A judge that cannot parse its own reply must not score the attempt 0.

The judge used to answer "No" to all nine checklist questions once its retries
ran out. That scored D1, D2 and D3 at 0.0 and wrote Diagnosis.success=False, so
an attempt the judge never scored looked exactly like a diagnosis it scored and
rejected.

These tests pin the replacement: retries that actually correct the model, then
an unscored report carrying the reason and the judge's last reply.
"""

from __future__ import annotations

import pytest

from sregym.conductor.oracles.llm_as_a_judge.judge import (
    UNSCORED_BACKEND_UNAVAILABLE,
    UNSCORED_JUDGE_PARSE_FAILURE,
    DiagnosisJudge,
    JudgeParseError,
)
from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle

GROUND_TRUTH = "The checkout deployment has a misconfigured port."
DIAGNOSIS = "checkout is misconfigured: PRODUCT_CATALOG_ADDR points at port 8082."


class _Reply:
    def __init__(self, content: str):
        self.content = content


class _FakeBackend:
    """A judge backend that returns canned replies, one per call."""

    def __init__(self, replies: list[str]):
        self._replies = list(replies)
        self.prompts: list[str] = []

    def inference(self, messages):
        self.prompts.append(messages[-1].content)
        return _Reply(self._replies.pop(0) if self._replies else self._replies_exhausted())

    @staticmethod
    def _replies_exhausted():
        raise AssertionError("backend called more times than the test scripted")


def _judge(replies: list[str]) -> tuple[DiagnosisJudge, _FakeBackend]:
    judge = DiagnosisJudge()
    backend = _FakeBackend(replies)
    judge._backend = backend
    return judge, backend


def _all_yes(judge: DiagnosisJudge) -> str:
    items = ", ".join(
        f'{{"id": "{qid}", "answer": "Yes", "evidence": "matches", "confidence": "High"}}'
        for qid in judge._all_question_ids
    )
    return f"[{items}]"


def test_unparseable_reply_raises_rather_than_answering_no_nine_times():
    judge, backend = _judge(["not json at all"] * DiagnosisJudge.MAX_PARSE_ATTEMPTS)

    with pytest.raises(JudgeParseError) as excinfo:
        judge._call_llm_with_retry("checklist prompt")

    assert excinfo.value.attempts == DiagnosisJudge.MAX_PARSE_ATTEMPTS
    assert excinfo.value.raw_response == "not json at all"
    assert len(backend.prompts) == DiagnosisJudge.MAX_PARSE_ATTEMPTS


def test_every_retry_carries_a_correction_naming_the_failure():
    """The old loop corrected only a missing-question reply, and only once.

    An "Invalid JSON" error matched neither of its two gates, so the case that
    most needs a format correction got the identical prompt resent at
    temperature 0.0.
    """
    judge, backend = _judge(["```not json```"] * DiagnosisJudge.MAX_PARSE_ATTEMPTS)

    with pytest.raises(JudgeParseError):
        judge._call_llm_with_retry("checklist prompt")

    assert backend.prompts[0] == "checklist prompt"
    for prompt in backend.prompts[1:]:
        assert "could not be parsed" in prompt
        assert f"exactly {judge._num_questions}" in prompt


def test_a_corrected_retry_still_scores_the_attempt():
    judge, backend = _judge([])
    backend._replies = ["garbage", _all_yes(judge)]

    report = judge.judge_detailed(DIAGNOSIS, GROUND_TRUTH)

    assert report.unscored_reason == ""
    assert report.composite_score == 1.0
    assert len(backend.prompts) == 2


def test_exhausted_retries_produce_an_unscored_report_not_a_zero():
    judge, _ = _judge(["garbage"] * DiagnosisJudge.MAX_PARSE_ATTEMPTS)

    report = judge.judge_detailed(DIAGNOSIS, GROUND_TRUTH)

    assert report.verdict is None
    assert report.unscored_reason == UNSCORED_JUDGE_PARSE_FAILURE
    assert report.raw_response == "garbage"
    # No dimensions at all, rather than three scored 0.0.
    assert report.dimensions == []


def test_a_missing_backend_is_a_different_unscored_reason(monkeypatch):
    monkeypatch.setattr(DiagnosisJudge, "backend", property(lambda self: None))
    judge = DiagnosisJudge()

    report = judge.judge_detailed(DIAGNOSIS, GROUND_TRUTH)

    assert report.verdict is None
    assert report.unscored_reason == UNSCORED_BACKEND_UNAVAILABLE


def test_an_empty_submission_is_still_a_graded_zero():
    """The agent submitting nothing is a real failure, not an unscored attempt."""
    judge, _ = _judge([])

    report = judge.judge_detailed("   ", GROUND_TRUTH)

    assert report.verdict is not None
    assert report.composite_score == 0.0
    assert report.unscored_reason == ""


def test_the_oracle_records_the_reason_and_the_raw_reply():
    """The row the results CSV is built from.

    Diagnosis.success empty is what keeps the attempt out of a graded pass
    rate; unscored_reason and judge_raw_response are what let a reader say
    why, and re-judge it, after the run.
    """
    oracle = LLMAsAJudgeOracle(problem=None, expected=GROUND_TRUTH)
    oracle.judge._backend = _FakeBackend(["garbage"] * DiagnosisJudge.MAX_PARSE_ATTEMPTS)

    results = oracle.evaluate(DIAGNOSIS)

    assert results["success"] is None
    assert results["accuracy"] is None
    assert results["unscored_reason"] == UNSCORED_JUDGE_PARSE_FAILURE
    assert results["judge_raw_response"] == "garbage"
    assert results["checklist"] == []
