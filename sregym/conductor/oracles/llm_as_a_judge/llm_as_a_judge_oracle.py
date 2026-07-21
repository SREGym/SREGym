"""LLM-as-a-Judge Oracle for evaluating agent solutions using LLM judgment."""

from sregym.conductor.oracles.base import Oracle
from sregym.conductor.oracles.llm_as_a_judge.judge import DiagnosisJudge, JudgmentResult


class LLMAsAJudgeOracle(Oracle):
    """Oracle that uses an LLM judge to evaluate agent solutions against expected root causes."""

    def __init__(
        self,
        problem,
        expected: str,
        provider: str | None = None,
        model_name: str | None = None,
        url: str | None = None,
        api_key: str | None = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
        minimum_dimension_scores: dict[str, float] | None = None,
    ):
        super().__init__(problem)
        self.expected = expected if expected else ""
        self.minimum_dimension_scores = minimum_dimension_scores or {}
        invalid_scores = {
            dimension_id: score
            for dimension_id, score in self.minimum_dimension_scores.items()
            if not 0.0 <= score <= 1.0
        }
        if invalid_scores:
            raise ValueError(f"dimension score floors must be between 0 and 1: {invalid_scores}")

        # Initialize the LLM judge
        self.judge = DiagnosisJudge(
            provider=provider,
            model_name=model_name,
            url=url,
            api_key=api_key,
            temperature=temperature,
            max_tokens=max_tokens,
        )

    def evaluate(self, solution, duration=None) -> dict:
        """Evaluate the agent's diagnosis.

        Parameters
        ----------
        solution : str
            The agent's submitted diagnosis text.
        duration : float, optional
            Wall-clock time the agent took (currently unused by the judge but
            accepted for interface compatibility with the base ``Oracle``).
        """
        print("== LLM-as-a-Judge Evaluation ==")
        results = {}

        # Normalize solution to string
        if not isinstance(solution, str):
            solution = str(solution)

        try:
            # Get detailed judgment from DiagnosisJudge using root-cause-only ground truth
            report = self.judge.judge_detailed(
                solution=solution,
                expectation=self.expected,
            )

            # Check if judge is not initialized
            if report.verdict is None:
                print("⚠️  LLM judge is not initialized - returning null result")
                results["judgment"] = None
                results["reasoning"] = report.reasoning
                results["success"] = None
                results["accuracy"] = None
                results["checklist"] = []
                return results

            # Use composite score (0.0-1.0) scaled to 0-100
            acc = round(report.composite_score * 100.0, 2)
            dimension_scores = {dimension.dimension_id: dimension.score for dimension in report.dimensions}
            unmet_dimension_scores = {
                dimension_id: (dimension_scores.get(dimension_id, 0.0), minimum_score)
                for dimension_id, minimum_score in self.minimum_dimension_scores.items()
                if dimension_scores.get(dimension_id, 0.0) < minimum_score
            }
            is_correct = report.verdict == JudgmentResult.TRUE and not unmet_dimension_scores
            effective_verdict = JudgmentResult.TRUE if is_correct else JudgmentResult.FALSE
            reasoning = report.reasoning
            if unmet_dimension_scores:
                detail = ", ".join(
                    f"{dimension_id}={actual:.2f} (requires {minimum:.2f})"
                    for dimension_id, (actual, minimum) in unmet_dimension_scores.items()
                )
                reasoning = f"{reasoning} Required dimension score not met: {detail}."

            if is_correct:
                print(f"✅ Correct diagnosis: {effective_verdict.value} (score: {acc:.1f}/100)")
            else:
                print(f"❌ Incorrect diagnosis: {effective_verdict.value} (score: {acc:.1f}/100)")
                if unmet_dimension_scores:
                    print(f"   Required dimension score not met: {detail}")
                print(
                    f"   Expected: {self.expected[:100]}..."
                    if len(self.expected) > 100
                    else f"   Expected: {self.expected}"
                )
                print(f"   Got: {solution[:100]}..." if len(solution) > 100 else f"   Got: {solution}")

            # Include dimension breakdown in results
            results["judgment"] = effective_verdict.value
            results["reasoning"] = reasoning
            results["success"] = is_correct
            results["accuracy"] = acc
            results["composite_score"] = report.composite_score
            results["dimensions"] = {
                dim.dimension_id: {
                    "name": dim.dimension_name,
                    "score": dim.score,
                }
                for dim in report.dimensions
            }
            results["checklist"] = [
                {
                    "id": q.question_id,
                    "answer": "Yes" if q.answer else "No",
                    "evidence": q.evidence,
                    "confidence": q.confidence,
                }
                for dim in report.dimensions
                for q in dim.questions
            ]

        except Exception as e:
            print(f"❌ Error during LLM judgment: {e}")
            results["judgment"] = "Error"
            results["reasoning"] = f"Error: {str(e)}"
            results["success"] = False
            results["accuracy"] = 0.0
            results["checklist"] = []
            results["error"] = str(e)

        return results
