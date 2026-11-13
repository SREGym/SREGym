"""Regression tests for the checklist judge's handling of truncated LLM output.

Reasoning models count hidden thinking tokens against ``max_tokens``. When the
budget runs out the visible JSON array stops mid-string, the provider reports
``finish_reason=length``, and every checklist question used to default to "No".
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from sregym.conductor.oracles.llm_as_a_judge import judge as judge_mod
from sregym.conductor.oracles.llm_as_a_judge.judge import DEFAULT_JUDGE_MAX_TOKENS, DiagnosisJudge, JudgeParseError


class _FakeBackend:
    """Return a truncated array until ``max_tokens`` is at least ``needed``."""

    def __init__(self, full_text: str, needed: int, max_tokens: int):
        self.full_text = full_text
        self.needed = needed
        self.max_tokens = max_tokens
        self.calls: list[int] = []

    def inference(self, messages):
        self.calls.append(self.max_tokens)
        if self.max_tokens < self.needed:
            cut = self.full_text[: len(self.full_text) // 3]
            return SimpleNamespace(content=cut, response_metadata={"finish_reason": "length"})
        return SimpleNamespace(content=self.full_text, response_metadata={"finish_reason": "stop"})


def _full_answer(judge: DiagnosisJudge) -> str:
    return json.dumps(
        [
            {"id": qid, "answer": "Yes", "evidence": "matches the ground truth", "confidence": "High"}
            for qid in judge._all_question_ids
        ]
    )


def test_default_budget_fits_reasoning_models():
    assert DiagnosisJudge().max_tokens == DEFAULT_JUDGE_MAX_TOKENS
    assert DEFAULT_JUDGE_MAX_TOKENS >= 16384


def test_truncated_response_retries_with_larger_budget():
    judge = DiagnosisJudge(max_tokens=1024)
    backend = _FakeBackend(_full_answer(judge), needed=4096, max_tokens=1024)
    judge._backend = backend

    results = judge._call_llm_with_retry("diagnosis")

    assert backend.calls == [1024, 1024 * judge_mod._TRUNCATION_RETRY_FACTOR]
    assert judge.max_tokens == 1024 * judge_mod._TRUNCATION_RETRY_FACTOR
    assert all(item["answer"] == "Yes" for item in results)
    assert not any("parse failure" in item["evidence"] for item in results)


def test_non_truncated_parse_failure_keeps_budget():
    judge = DiagnosisJudge(max_tokens=1024)
    bad = SimpleNamespace(content="not json at all", response_metadata={"finish_reason": "stop"})
    calls: list[int] = []

    class _Backend:
        max_tokens = 1024

        def inference(self, messages):
            calls.append(self.max_tokens)
            return bad

    judge._backend = _Backend()
    with pytest.raises(JudgeParseError):
        judge._call_llm_with_retry("diagnosis")

    assert calls == [1024] * DiagnosisJudge.MAX_PARSE_ATTEMPTS
    assert judge.max_tokens == 1024


def test_was_truncated_reads_finish_reason():
    assert DiagnosisJudge._was_truncated(SimpleNamespace(response_metadata={"finish_reason": "length"}))
    assert not DiagnosisJudge._was_truncated(SimpleNamespace(response_metadata={"finish_reason": "stop"}))
    assert not DiagnosisJudge._was_truncated(SimpleNamespace(response_metadata={}))
    assert not DiagnosisJudge._was_truncated(None)


def test_the_budget_grows_once_and_a_second_truncation_gets_the_correction():
    """Growth sticks for every later judge call, and a second step would
    exceed most providers' output limit, so the second truncation is
    retried like any other parse failure."""
    judge = DiagnosisJudge(max_tokens=1024)
    backend = _FakeBackend(_full_answer(judge), needed=10**9, max_tokens=1024)
    judge._backend = backend

    with pytest.raises(JudgeParseError):
        judge._call_llm_with_retry("diagnosis")

    grown = 1024 * judge_mod._TRUNCATION_RETRY_FACTOR
    assert backend.calls == [1024, grown, grown]
    assert judge.max_tokens == grown


def test_budget_growth_does_not_repeat_for_later_causes():
    judge = DiagnosisJudge(max_tokens=1024)
    backend = _FakeBackend(_full_answer(judge), needed=10**9, max_tokens=1024)
    judge._backend = backend

    for _cause in range(2):
        with pytest.raises(JudgeParseError):
            judge._call_llm_with_retry("diagnosis")

    assert backend.calls == [1024, 4096, 4096, 4096, 4096, 4096]
    assert judge.max_tokens == backend.max_tokens == 4096


def test_final_parse_failure_does_not_grow_an_unused_budget():
    judge = DiagnosisJudge(max_tokens=1024)
    replies = iter(
        [
            SimpleNamespace(content="not JSON", response_metadata={"finish_reason": "stop"}),
            SimpleNamespace(content="still not JSON", response_metadata={"finish_reason": "stop"}),
            SimpleNamespace(content='[{"id":', response_metadata={"finish_reason": "length"}),
        ]
    )
    backend = SimpleNamespace(max_tokens=1024, inference=lambda _messages: next(replies))
    judge._backend = backend

    with pytest.raises(JudgeParseError):
        judge._call_llm_with_retry("diagnosis")

    assert judge.max_tokens == backend.max_tokens == 1024
