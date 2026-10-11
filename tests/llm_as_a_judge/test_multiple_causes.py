from unittest.mock import Mock

import pytest

from sregym.conductor.oracles.llm_as_a_judge.judge import JudgmentResult
from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.oracles.llm_as_a_judge.models import DimensionResult, JudgmentReport, QuestionResult


def report(score=1.0, verdict=JudgmentResult.TRUE):
    return JudgmentReport(
        verdict=verdict,
        reasoning="Recorded evaluation",
        composite_score=score,
        dimensions=[
            DimensionResult(
                dimension_id="D2",
                dimension_name="Fault Characterization",
                score=score,
                questions=[QuestionResult("D2-Q1", "Correct mechanism?", True, "Matching mechanism", "High")],
            )
        ],
    )


@pytest.mark.parametrize("as_list", [False, True])
def test_single_cause_keeps_original_request_and_report(as_list):
    cause = "checkout points to port 8082 instead of 8080"
    expected = [cause] if as_list else cause
    oracle = LLMAsAJudgeOracle(None, expected)
    if as_list:
        expected[0] = "must not modify the configured reference"
    oracle.judge.judge_detailed = Mock(return_value=report())

    result = oracle.evaluate("The checkout port is wrong", duration=12)

    oracle.judge.judge_detailed.assert_called_once_with(solution="The checkout port is wrong", expectation=cause)
    assert result["success"] is True
    assert result["accuracy"] == 100
    assert result["judgment"] == "True"
    assert result["composite_score"] == 1
    assert result["dimensions"]["D2"]["score"] == 1
    assert result["checklist"][0]["id"] == "D2-Q1"
    assert "oracles" not in result


@pytest.mark.parametrize("count", [2, 3, 4])
def test_every_cause_receives_full_answer_and_scoped_context(count):
    causes = [f"service-{i} has fault-{i}" for i in range(count)]
    oracle = LLMAsAJudgeOracle(None, causes)
    oracle.judge.judge_detailed = Mock(return_value=report())
    causes.append("must not modify the configured reference")

    result = oracle.evaluate("The complete submitted answer", duration=8)

    assert result["success"] is True
    assert result["accuracy"] == 100
    assert len(result["oracles"]) == count
    for i, call in enumerate(oracle.judge.judge_detailed.call_args_list, 1):
        assert call.kwargs["solution"] == "The complete submitted answer"
        expectation = call.kwargs["expectation"]
        assert expectation.endswith(f"Evaluation target — Cause {i}:\n{causes[i - 1]}")
        assert all(cause in expectation for cause in causes[:count])
        assert "other known causes are graded separately" in expectation
        assert "cannot satisfy a question about this target" in expectation
        assert causes[-1] not in expectation
        assert result["oracles"][i - 1]["checklist"][0]["id"] == "D2-Q1"


@pytest.mark.parametrize("count", [2, 3, 4])
def test_one_failed_cause_fails_and_reports_the_lowest_score(count):
    oracle = LLMAsAJudgeOracle(None, [f"cause-{i}" for i in range(count)])
    oracle.judge.judge_detailed = Mock(side_effect=[report()] * (count - 1) + [report(0.67, JudgmentResult.FALSE)])

    result = oracle.evaluate("An incomplete diagnosis")

    assert result["accuracy"] == 67
    assert result["success"] is False
    assert len(result["oracles"]) == count
    assert result["oracles"][-1]["success"] is False


def test_judge_error_does_not_skip_remaining_causes():
    oracle = LLMAsAJudgeOracle(None, ["cause A", "cause B", "cause C"])
    oracle.judge.judge_detailed = Mock(side_effect=[report(), ValueError("invalid judge response"), report()])

    result = oracle.evaluate("A diagnosis")

    assert result["success"] is False
    assert result["accuracy"] == 0
    assert len(result["oracles"]) == 3
    assert result["oracles"][1]["error"] == "invalid judge response"
    assert result["oracles"][2]["success"] is True


@pytest.mark.parametrize("verdict,success", [(JudgmentResult.TRUE, None), (JudgmentResult.FALSE, False), (None, None)])
@pytest.mark.parametrize("reverse", [False, True])
def test_unavailable_judge_preserves_incomplete_unless_another_cause_fails(verdict, success, reverse):
    oracle = LLMAsAJudgeOracle(None, ["cause A", "cause B"])
    reports = [report(1 if verdict == JudgmentResult.TRUE else 0, verdict), report(0, None)]
    oracle.judge.judge_detailed = Mock(side_effect=reports[::-1] if reverse else reports)

    result = oracle.evaluate("A diagnosis")

    assert result["success"] is success
    assert result["accuracy"] is None
    assert result["oracles"][0 if reverse else 1]["success"] is None
    assert result["oracles"][0 if reverse else 1]["accuracy"] is None
    assert oracle.judge.judge_detailed.call_count == 2


def test_failed_cause_log_displays_the_cause_instead_of_the_shared_preamble(capsys):
    oracle = LLMAsAJudgeOracle(None, ["checkout has the wrong port", "database has the wrong isolation"])
    oracle.judge.judge_detailed = Mock(side_effect=[report(), report(0, JudgmentResult.FALSE)])

    oracle.evaluate("A diagnosis")

    output = capsys.readouterr().out
    assert "Expected: database has the wrong isolation" in output
    assert "Expected: This incident" not in output


@pytest.mark.parametrize("expected", [[], [""], ["cause", "   "], [None], ["cause", 3]])
def test_invalid_cause_lists_are_rejected(expected):
    with pytest.raises(ValueError, match="nonempty"):
        LLMAsAJudgeOracle(None, expected)


@pytest.mark.parametrize("expected", [("cause A", "cause B"), {"cause": "A"}, 3])
def test_unsupported_cause_types_are_rejected(expected):
    with pytest.raises(TypeError, match="string or a list"):
        LLMAsAJudgeOracle(None, expected)


@pytest.mark.parametrize("expected", [None, ""])
def test_missing_root_cause_keeps_the_previous_empty_expectation(expected):
    oracle = LLMAsAJudgeOracle(None, expected)
    oracle.judge.judge_detailed = Mock(return_value=report())

    oracle.evaluate("A diagnosis")

    assert oracle.expected == ""
    oracle.judge.judge_detailed.assert_called_once_with(solution="A diagnosis", expectation="")
