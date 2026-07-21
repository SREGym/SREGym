from types import SimpleNamespace
from unittest.mock import Mock

from sregym.conductor.oracles.llm_as_a_judge.judge import JudgmentResult
from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle


def _oracle(*, dimension_score: float, minimum_score: float | None):
    oracle = object.__new__(LLMAsAJudgeOracle)
    oracle.expected = "the retry feedback loop"
    oracle.minimum_dimension_scores = {} if minimum_score is None else {"D2": minimum_score}
    oracle.judge = SimpleNamespace(
        judge_detailed=Mock(
            return_value=SimpleNamespace(
                verdict=JudgmentResult.TRUE,
                composite_score=0.78,
                reasoning="Verdict: True",
                dimensions=[
                    SimpleNamespace(
                        dimension_id="D2",
                        dimension_name="Fault Characterization",
                        score=dimension_score,
                        questions=[],
                    )
                ],
            )
        )
    )
    return oracle


def test_default_oracle_uses_the_composite_judge_verdict():
    result = _oracle(dimension_score=0.33, minimum_score=None).evaluate("diagnosis")

    assert result["success"] is True
    assert result["judgment"] == "True"


def test_oracle_can_require_a_minimum_causal_characterization_score():
    result = _oracle(dimension_score=0.33, minimum_score=0.67).evaluate("diagnosis")

    assert result["success"] is False
    assert result["judgment"] == "False"
    assert "D2=0.33 (requires 0.67)" in result["reasoning"]
