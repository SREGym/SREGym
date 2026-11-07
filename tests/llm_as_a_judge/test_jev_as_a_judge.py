"""Jev-as-a-Judge modes (jev_as_a_judge.py) and their wiring into LLMAsAJudgeOracle, with fake Jev and LLM judges."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from sregym.conductor.oracles.llm_as_a_judge.jev_as_a_judge import (
    QUESTIONS,
    JevAsAJudge,
    judge_mode,
    parse_expectation,
    question_key,
)
from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle

EXPECTED = "[fault_spec] component=deployment/cart; namespace=shop || cart reads the wrong REDIS_ADDR port."


class FakeJev:
    """system_one answers every checklist question with `p`."""

    def __init__(self, p: float, fail: bool = False):
        """Answer every checklist question with `p`; raise on every request when `fail`."""
        self.p, self.fail = p, fail
        self.calls: list[tuple[dict, dict]] = []

    def system_one(self, state, questions):
        """Record the request and answer it the way TypeSafe's system_one does."""
        self.calls.append((state, questions))
        if self.fail:
            raise RuntimeError("jev down")
        return SimpleNamespace(answers={k: SimpleNamespace(noul=self.p) for k in questions})


def oracle(mode: str, jev: FakeJev, monkeypatch, llm_success: bool = True) -> tuple[LLMAsAJudgeOracle, list]:
    """An oracle in `mode` on the fake Jev, with the LLM judge replaced by a recorder that returns `llm_success`."""
    o = LLMAsAJudgeOracle(problem=None, expected=EXPECTED, mode=mode, jev_as_a_judge=JevAsAJudge(client=jev))
    llm_calls: list[str] = []

    def fake_llm(solution):
        """Record the diagnosis and return the fixed LLM verdict."""
        llm_calls.append(solution)
        return {"judgment": str(llm_success), "success": llm_success, "accuracy": 100.0 if llm_success else 0.0}

    monkeypatch.setattr(o, "_evaluate_llm", fake_llm)
    return o, llm_calls


def test_parse_expectation_splits_fault_spec():
    """The fault spec splits into component, namespace and description; plain text is all description."""
    assert parse_expectation(EXPECTED) == {
        "component": "deployment/cart",
        "namespace": "shop",
        "description": "cart reads the wrong REDIS_ADDR port.",
    }
    assert parse_expectation("plain text") == {"description": "plain text"}


def test_request_has_the_nine_checklist_questions():
    """One request carries the fault spec, the diagnosis and the 9 checklist questions, and nothing else."""
    jev = FakeJev(0.9)
    JevAsAJudge(client=jev).ask("cart has the wrong port", EXPECTED)
    state, questions = jev.calls[0]
    assert state == {"ground_truth": parse_expectation(EXPECTED), "diagnosis": "cart has the wrong port"}
    assert list(questions) == [question_key(q) for q in QUESTIONS]
    assert len(questions) == 9


def test_score_uses_checklist_weights():
    """The score weighs dimensions by the checklist weights."""
    judge = JevAsAJudge(client=FakeJev(0.0))
    p = {q: 0.0 for q in QUESTIONS} | {"D1-Q1": 1.0, "D1-Q2": 1.0, "D1-Q3": 1.0}
    score, dims = judge.score(p)
    assert score == 0.33
    assert dims == {"D1": 1.0, "D2": 0.0, "D3": 0.0}


@pytest.mark.parametrize(
    "p, sent, passed",
    [
        (0.90, False, True),  # above the band: Jev passes it
        (0.20, False, False),  # below the band: Jev fails it
        (0.60, True, None),  # inside the band: the LLM decides
        (0.7522, True, None),  # the band is closed at both ends
        (0.3278, True, None),
        (0.7523, False, True),  # just outside the band: Jev decides
        (0.3277, False, False),
    ],
)
def test_mixed_rule(p, sent, passed):
    """Mixed mode keeps Jev's verdict outside the band and sends the band to the LLM."""
    verdict = JevAsAJudge(client=FakeJev(p)).judge("diagnosis", EXPECTED, "mixed")
    assert (verdict.send_to_llm, verdict.passed) == (sent, passed)


def test_jev_alone_cut():
    """Jev alone passes at the cut and never sends a report to the LLM."""
    assert JevAsAJudge(client=FakeJev(0.6199)).judge("d", EXPECTED, "jev").passed is False
    verdict = JevAsAJudge(client=FakeJev(0.62)).judge("d", EXPECTED, "jev")
    assert verdict.passed is True and verdict.send_to_llm is False


def test_empty_diagnosis_fails_without_a_request():
    """An empty diagnosis fails without asking Jev."""
    jev = FakeJev(0.9)
    verdict = JevAsAJudge(client=jev).judge("  ", EXPECTED, "mixed")
    assert (verdict.passed, verdict.send_to_llm, jev.calls) == (False, False, [])


def test_judge_mode_env(monkeypatch):
    """JUDGE_MODE defaults to llm, ignores case and rejects unknown modes."""
    monkeypatch.delenv("JUDGE_MODE", raising=False)
    assert judge_mode() == "llm"
    monkeypatch.setenv("JUDGE_MODE", "Mixed")
    assert judge_mode() == "mixed"
    monkeypatch.setenv("JUDGE_MODE", "luna")
    with pytest.raises(ValueError):
        judge_mode()


def test_oracle_mixed_keeps_jev_verdict_outside_the_band(monkeypatch):
    """The oracle returns Jev's verdict in the LLM judge's shape when the score is outside the band."""
    o, llm_calls = oracle("mixed", FakeJev(0.9), monkeypatch)
    r = o.evaluate("cart has the wrong REDIS_ADDR port")
    assert r["success"] is True and r["judgment"] == "True" and r["accuracy"] == 90.0
    assert r["jev"]["sent_to_llm"] is False and llm_calls == []
    assert len(r["checklist"]) == 9


def test_oracle_mixed_sends_unsure_reports_to_the_llm(monkeypatch):
    """The oracle returns the LLM verdict for a report in the band and keeps Jev's answers under `jev`."""
    o, llm_calls = oracle("mixed", FakeJev(0.6), monkeypatch, llm_success=False)
    r = o.evaluate("maybe cart, maybe redis")
    assert r["success"] is False and llm_calls == ["maybe cart, maybe redis"]
    assert r["jev"]["sent_to_llm"] is True and r["jev"]["score"] == 0.6


def test_oracle_mixed_falls_back_to_the_llm_when_jev_fails(monkeypatch):
    """In mixed mode a failed Jev request leaves the verdict to the LLM judge."""
    o, llm_calls = oracle("mixed", FakeJev(0.9, fail=True), monkeypatch)
    r = o.evaluate("cart")
    assert r["success"] is True and llm_calls == ["cart"]
    assert r["jev"]["error"] == "jev down"


def test_oracle_jev_alone_never_asks_the_llm(monkeypatch):
    """In jev mode the LLM judge is never called, even when the Jev request fails."""
    o, llm_calls = oracle("jev", FakeJev(0.6), monkeypatch)
    r = o.evaluate("cart")
    assert r["success"] is False and llm_calls == []
    o, llm_calls = oracle("jev", FakeJev(0.9, fail=True), monkeypatch)
    r = o.evaluate("cart")
    assert r["judgment"] == "Error" and r["success"] is False and llm_calls == []


def test_oracle_llm_mode_does_not_build_a_jev_as_a_judge(monkeypatch):
    """The default llm mode builds no Jev-as-a-Judge."""
    monkeypatch.delenv("JUDGE_MODE", raising=False)
    o = LLMAsAJudgeOracle(problem=None, expected=EXPECTED)
    assert o.mode == "llm" and o.jev is None
