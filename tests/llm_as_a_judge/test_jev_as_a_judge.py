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
    supports_expectation,
)
from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle

EXPECTED = "[fault_spec] component=deployment/cart; namespace=shop || cart reads the wrong REDIS_ADDR port."
CALIBRATED_MODEL = "jev-1.14.0"
UNSUPPORTED_EXPECTATIONS = [
    "plain text",
    "plain text with a shell command: first || second",
    "Fault 1:\n" + EXPECTED,
    EXPECTED + "\nFault 2:\n[fault_spec] component=deployment/frontend; namespace=hotel || Bad image tag.",
    "[fault_spec] component=deployment/cart || Wrong Redis port.",
    "[fault_spec] namespace=shop || Wrong Redis port.",
    "[fault_spec] component=deployment/cart; namespace=shop || ",
    "[fault_spec] component=deployment/cart; component=deployment/redis; namespace=shop || Wrong Redis port.",
    "[fault_spec] component=deployment/cart; namespace=shop; extra=unknown || Wrong Redis port.",
]


class FakeJev:
    """system_one answers every checklist question with `p`."""

    def __init__(self, p: float, fail: bool = False, model: str = CALIBRATED_MODEL):
        """Answer every checklist question with `p`; raise on every request when `fail`."""
        self.p, self.fail = p, fail
        self.model = model
        self.calls: list[tuple[dict, dict]] = []
        self.requested_models: list[str] = []

    def system_one(self, state, questions, *, model):
        """Record the request and answer it the way TypeSafe's system_one does."""
        self.calls.append((state, questions))
        self.requested_models.append(model)
        if self.fail:
            raise RuntimeError("jev down")
        return SimpleNamespace(model=self.model, answers={k: SimpleNamespace(noul=self.p) for k in questions})


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
    assert jev.requested_models == [CALIBRATED_MODEL]


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


@pytest.mark.parametrize("model", ["jev-latest", "jev-preview", "jev-1.13.0"])
def test_uncalibrated_model_override_is_rejected(monkeypatch, model):
    monkeypatch.setenv("JUDGE_JEV_MODEL", model)
    with pytest.raises(ValueError, match="must match the calibrated model"):
        JevAsAJudge(client=FakeJev(0.9))


def test_calibrated_override_is_allowed_and_resolved_model_is_recorded(monkeypatch):
    monkeypatch.setenv("JUDGE_JEV_MODEL", CALIBRATED_MODEL)
    o, _ = oracle("mixed", FakeJev(0.9), monkeypatch)
    result = o.evaluate("cart uses the wrong port")
    assert result["jev"]["model"] == CALIBRATED_MODEL
    assert result["jev"]["requested_model"] == CALIBRATED_MODEL
    assert result["jev"]["model_release"] == "2026-10-22T02:00:00+00:00"


@pytest.mark.parametrize("mode", ["jev", "mixed"])
def test_other_response_model_cannot_use_the_calibrated_thresholds(monkeypatch, mode):
    o, llm_calls = oracle(mode, FakeJev(0.99, model="jev-1.13.0"), monkeypatch)
    result = o.evaluate("cart uses the wrong port")
    assert result["jev"]["model"] == "jev-1.13.0"
    assert result["jev"]["requested_model"] == CALIBRATED_MODEL
    assert "calibrated thresholds require" in result["jev"]["error"]
    assert "score" not in result["jev"]
    if mode == "jev":
        assert result["judgment"] == "Error" and llm_calls == []
    else:
        assert llm_calls == ["cart uses the wrong port"] and result["jev"]["sent_to_llm"]


def test_empty_diagnosis_does_not_reuse_previous_response_model(monkeypatch):
    o, _ = oracle("jev", FakeJev(0.9), monkeypatch)
    assert o.evaluate("cart uses the wrong port")["jev"]["model"] == CALIBRATED_MODEL
    result = o.evaluate(" ")
    assert result["success"] is False and result["jev"]["model"] is None


def test_jev_only_does_not_construct_an_llm_judge(monkeypatch):
    def forbidden(**_):
        pytest.fail("Jev-only mode must not construct an LLM judge")

    monkeypatch.setattr("sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle.DiagnosisJudge", forbidden)
    o = LLMAsAJudgeOracle(None, EXPECTED, mode="JEV", jev_as_a_judge=JevAsAJudge(client=FakeJev(0.9)))
    assert o.judge is None and o.evaluate("cart uses the wrong port")["success"] is True


@pytest.mark.parametrize("response_model", [CALIBRATED_MODEL, "jev-1.13.0"])
def test_native_sdk_transport_checks_resolved_model_without_network(response_model):
    import json

    import httpx2
    from typesafe_sdk import RetryPolicy, TypeSafeClient

    requests = []

    def respond(request):
        body = json.loads(request.content)
        requests.append(body)
        return httpx2.Response(
            200,
            json={
                "model": response_model,
                "usage": {"input_tokens": 10, "output_tokens": 9},
                "answers": {key: {"type": "noul", "noul": 0.9} for key in body["questions"]},
            },
        )

    with TypeSafeClient(
        api_key="test-placeholder", retry=RetryPolicy(max_retries=0), transport=httpx2.MockTransport(respond)
    ) as client:
        judge = JevAsAJudge(client=client)
        if response_model == CALIBRATED_MODEL:
            verdict = judge.judge("cart uses the wrong port", EXPECTED, "jev")
            assert verdict.passed and verdict.model == CALIBRATED_MODEL
        else:
            with pytest.raises(ValueError, match="calibrated thresholds require"):
                judge.judge("cart uses the wrong port", EXPECTED, "jev")
    assert requests[0]["model"] == CALIBRATED_MODEL


@pytest.mark.parametrize("expected", UNSUPPORTED_EXPECTATIONS)
def test_unsupported_reference_is_preserved_without_a_partial_parse(expected):
    assert parse_expectation(expected) == {"description": expected.strip()}
    assert supports_expectation(expected) is False


@pytest.mark.parametrize("expected", UNSUPPORTED_EXPECTATIONS)
def test_mixed_mode_sends_unsupported_references_to_the_llm_without_asking_jev(monkeypatch, expected):
    jev = FakeJev(0.99)
    o = LLMAsAJudgeOracle(None, expected, mode="mixed", jev_as_a_judge=JevAsAJudge(client=jev))
    calls = []

    def evaluate(solution):
        calls.append((solution, o.expected))
        return {"success": False, "judgment": "False"}

    monkeypatch.setattr(o, "_evaluate_llm", evaluate)
    result = o.evaluate("the submitted diagnosis")
    assert calls == [("the submitted diagnosis", expected)]
    assert jev.calls == [] and result["success"] is False
    assert result["jev"]["skip_reason"] == "unsupported_expectation"
    assert result["jev"]["sent_to_llm"] is True and result["jev"]["model"] is None
    assert "score" not in result["jev"]


@pytest.mark.parametrize("expected", UNSUPPORTED_EXPECTATIONS)
def test_jev_only_rejects_unsupported_references_during_construction(monkeypatch, expected):
    def forbidden(**_):
        pytest.fail("Unsupported Jev-only input must not construct an LLM judge")

    monkeypatch.setattr("sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle.DiagnosisJudge", forbidden)
    jev = FakeJev(0.99)
    with pytest.raises(ValueError, match="Use --judge-mode mixed or llm"):
        LLMAsAJudgeOracle(None, expected, mode="jev", jev_as_a_judge=JevAsAJudge(client=jev))
    assert jev.calls == []


def test_direct_jev_call_rejects_compound_input_without_a_request():
    jev = FakeJev(0.99)
    with pytest.raises(ValueError, match="one complete"):
        JevAsAJudge(client=jev).judge("the submitted diagnosis", UNSUPPORTED_EXPECTATIONS[3], "mixed")
    assert jev.calls == []


def test_actual_compound_problem_keeps_the_complete_reference_for_llm_grading(monkeypatch):
    from unittest.mock import Mock

    from sregym.conductor.oracles.llm_as_a_judge.judge import JudgmentResult
    from sregym.conductor.problems.base import Problem
    from sregym.conductor.problems.multiple_failures import MultipleIndependentFailures

    monkeypatch.setenv("JUDGE_MODE", "mixed")
    monkeypatch.setattr(
        "sregym.conductor.problems.multiple_failures.CompositeApp", lambda _: SimpleNamespace(namespace="shop")
    )
    second = Problem.build_structured_root_cause(
        component="deployment/frontend", namespace="hotel", description="frontend uses an unavailable image tag."
    )
    problems = [
        SimpleNamespace(app=SimpleNamespace(), namespace=namespace, root_cause=expected)
        for namespace, expected in [("shop", EXPECTED), ("hotel", second)]
    ]
    problem = MultipleIndependentFailures(problems)
    assert not supports_expectation(problem.root_cause)
    assert EXPECTED in problem.root_cause and second in problem.root_cause
    oracle = problem.diagnosis_oracle
    llm = Mock(
        return_value=SimpleNamespace(
            verdict=JudgmentResult.TRUE, composite_score=1.0, reasoning="both faults identified", dimensions=[]
        )
    )
    monkeypatch.setattr(oracle.judge, "judge_detailed", llm)
    monkeypatch.setattr(oracle.jev, "ask", lambda *_: pytest.fail("Compound references must bypass Jev"))
    submission = "cart has the wrong Redis port and frontend uses an unavailable image"
    result = oracle.evaluate(submission)
    assert result["success"] is True
    llm.assert_called_once_with(solution=submission, expectation=problem.root_cause)
    assert result["jev"]["skip_reason"] == "unsupported_expectation"
