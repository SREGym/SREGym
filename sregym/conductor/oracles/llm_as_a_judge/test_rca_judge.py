"""Test script for RCAJudge.

Loads a problem from root_causes.csv and runs RCAJudge.judge_detailed().

If OPENAI_API_KEY is set, uses a real OpenAI backend (gpt-4.1-mini).
Otherwise, falls back to a mock LLM response (no real API calls).
"""

import csv
import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, PropertyMock, patch

# Ensure the repo root is on sys.path so that `llm_backend` (a top-level
# directory not installed as a package) is importable regardless of how
# this script is invoked (e.g. `python test_rca_judge.py` from any cwd).
_REPO_ROOT = Path(__file__).parents[4]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
from sregym.conductor.oracles.llm_as_a_judge.judge import JudgmentResult, RCAJudge  # noqa: E402

HERE = Path(__file__).parent

# Detect whether we can use a real OpenAI backend
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
USE_OPENAI = bool(OPENAI_API_KEY)


def load_first_problem() -> dict:
    """Load the first entry from root_causes.csv."""
    csv_path = HERE / "root_causes.csv"
    with open(csv_path, newline="") as f:
        reader = csv.DictReader(f)
        return next(reader)


def _create_judge() -> RCAJudge:
    """Create a RCAJudge using OpenAI if available, otherwise mock provider."""
    if USE_OPENAI:
        return RCAJudge(
            provider="openai",
            model_name="gpt-4.1-mini",
            api_key=OPENAI_API_KEY,
        )
    return RCAJudge(provider="mock", model_name="mock-model")


def _create_openai_backend():
    """Create a real LiteLLMBackend configured for OpenAI."""
    from llm_backend.get_llm_backend import LiteLLMBackend

    return LiteLLMBackend(
        provider="openai",
        model_name="gpt-4.1-mini",
        api_key=OPENAI_API_KEY,
        temperature=0.0,
    )


def print_report(report) -> None:
    """Print a JudgmentReport in a human-readable format."""
    print(f"  Verdict:         {report.verdict}")
    print(f"  Composite score: {report.composite_score}")
    for dim in report.dimensions:
        print(f"  {dim.dimension_id} {dim.dimension_name}: {dim.score:.2f}")
        for q in dim.questions:
            print(f"    {q.question_id}: {'Yes' if q.answer else 'No'} ({q.confidence}) - {q.evidence}")


def build_mock_response(question_ids: list[str], all_yes: bool = True) -> str:
    """Build a JSON string mimicking the LLM checklist response."""
    results = []
    for qid in question_ids:
        results.append(
            {
                "id": qid,
                "answer": "Yes" if all_yes else "No",
                "evidence": f"Mock evidence for {qid}",
                "confidence": "High" if all_yes else "Low",
            }
        )
    return json.dumps(results)


def _patch_backend(judge, mock_llm):
    """Return a context manager that patches the backend property with mock_llm."""
    return patch.object(type(judge), "backend", new_callable=PropertyMock, return_value=mock_llm)


def _inject_backend(judge):
    """Inject a real or mock backend into the judge. Returns a context manager.

    For OpenAI: sets judge._backend directly and returns a no-op context manager.
    For mock (all_yes=True by default): patches the backend property.
    """
    if USE_OPENAI:
        judge._backend = _create_openai_backend()
        # Return a no-op context manager
        from contextlib import nullcontext

        return nullcontext()
    else:
        mock_json = build_mock_response(judge._all_question_ids, all_yes=True)
        mock_llm = MagicMock()
        mock_llm.inference.return_value = MagicMock(content=mock_json)
        return _patch_backend(judge, mock_llm)


def test_judge_all_yes():
    """All questions answered Yes → verdict should be TRUE (mock) or a real evaluation (OpenAI)."""
    problem = load_first_problem()
    mode = "OpenAI (gpt-4.1-mini)" if USE_OPENAI else "mock"
    print(f"[{mode}] Problem: {problem['file']} / {problem['class']}")
    print(f"Ground truth: {problem['root_cause']}")

    judge = _create_judge()

    with _inject_backend(judge):
        report = judge.judge_detailed(
            solution="The ad-service has a feature flag causing failures, resulting in service unavailability.",
            expectation=problem["root_cause"],
        )

    print_report(report)

    if not USE_OPENAI:
        assert report.verdict == JudgmentResult.TRUE, f"Expected TRUE, got {report.verdict}"
        assert report.composite_score == 1.0, f"Expected 1.0, got {report.composite_score}"
    else:
        # With a real LLM, just verify we got a valid report
        assert report.verdict in (JudgmentResult.TRUE, JudgmentResult.FALSE)
        assert 0.0 <= report.composite_score <= 1.0
    print("\n✓ test_judge_all_yes PASSED")


def test_judge_all_no():
    """All questions answered No → verdict should be FALSE."""
    problem = load_first_problem()

    judge = _create_judge()

    if USE_OPENAI:
        # With real OpenAI, give a clearly wrong diagnosis
        judge._backend = _create_openai_backend()
        report = judge.judge_detailed(
            solution="The database ran out of disk space on the primary node.",
            expectation=problem["root_cause"],
        )
        assert report.verdict in (JudgmentResult.TRUE, JudgmentResult.FALSE)
        assert 0.0 <= report.composite_score <= 1.0
    else:
        mock_json = build_mock_response(judge._all_question_ids, all_yes=False)
        mock_llm = MagicMock()
        mock_llm.inference.return_value = MagicMock(content=mock_json)
        with _patch_backend(judge, mock_llm):
            report = judge.judge_detailed(
                solution="I have no idea what happened.",
                expectation=problem["root_cause"],
            )
        assert report.verdict == JudgmentResult.FALSE, f"Expected FALSE, got {report.verdict}"
        assert report.composite_score == 0.0, f"Expected 0.0, got {report.composite_score}"

    print_report(report)
    print("✓ test_judge_all_no PASSED")


def test_judge_drop_in():
    """judge() returns (JudgmentResult, str) — same as LLMJudge."""
    problem = load_first_problem()

    judge = _create_judge()

    with _inject_backend(judge):
        verdict, reasoning = judge.judge(
            solution="The ad-service has a feature flag causing failures.",
            expectation=problem["root_cause"],
        )

    assert isinstance(verdict, JudgmentResult), f"Expected JudgmentResult, got {type(verdict)}"
    assert isinstance(reasoning, str), f"Expected str, got {type(reasoning)}"
    print(f"  Verdict:   {verdict}")
    print(f"  Reasoning: {reasoning}")
    print("✓ test_judge_drop_in PASSED")


def test_empty_solution():
    """Empty solution → FALSE with score 0.0, no LLM call."""
    judge = _create_judge()

    if USE_OPENAI:
        judge._backend = _create_openai_backend()
    mock_llm = MagicMock()

    # Even with OpenAI available, empty solution should short-circuit (no LLM call)
    with _patch_backend(judge, mock_llm):
        report = judge.judge_detailed(solution="", expectation="some root cause")

    mock_llm.inference.assert_not_called()
    assert report.verdict == JudgmentResult.FALSE
    assert report.composite_score == 0.0
    print_report(report)
    print("✓ test_empty_solution PASSED")


if __name__ == "__main__":
    print(f"Backend: {'OpenAI (gpt-4.1-mini)' if USE_OPENAI else 'mock'}\n")
    test_judge_all_yes()
    test_judge_all_no()
    test_judge_drop_in()
    test_empty_solution()
    print("\n✅ All tests passed.")
