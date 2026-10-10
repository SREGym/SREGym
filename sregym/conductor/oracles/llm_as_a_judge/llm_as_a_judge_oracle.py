"""LLM-as-a-Judge Oracle for evaluating agent solutions using LLM judgment."""

from sregym.conductor.oracles.base import Oracle
from sregym.conductor.oracles.llm_as_a_judge.judge import (
    DEFAULT_JUDGE_MAX_TOKENS,
    DiagnosisJudge,
    JudgmentResult,
)


class LLMAsAJudgeOracle(Oracle):
    """Oracle that uses an LLM judge to evaluate agent solutions against expected root causes."""

    def __init__(
        self,
        problem,
        expected: str | list[str],
        provider: str | None = None,
        model_name: str | None = None,
        url: str | None = None,
        api_key: str | None = None,
        temperature: float = 0.0,
        max_tokens: int = DEFAULT_JUDGE_MAX_TOKENS,
    ):
        super().__init__(problem)
        if isinstance(expected, list) and (
            not expected or any(not isinstance(cause, str) or not cause.strip() for cause in expected)
        ):
            raise ValueError("Expected root causes must be a nonempty list of nonempty strings")
        self.expected = expected.copy() if isinstance(expected, list) else expected or ""

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

        A list of expected causes is graded separately with shared incident
        context. Every cause must pass; mean accuracy is informational only.
        Per-cause reports retain the original checklist and any judge errors.
        """
        if isinstance(self.expected, list):
            context = "\n\n".join(f"Cause {i}:\n{cause}" for i, cause in enumerate(self.expected, 1))
            reports = []
            for i, cause in enumerate(self.expected, 1):
                expectation = (
                    f"This incident has {len(self.expected)} independent root causes. "
                    f"Apply every checklist question ONLY to Cause {i}, the evaluation target below. "
                    "The other known causes are graded separately. Correct mentions of them are not "
                    "unrelated faults or over-attribution, and omitting them must not lower this target's score. "
                    "Evidence about another cause cannot satisfy a question about this target. "
                    "Still penalize incorrect claims and causes not supported by the full incident context.\n\n"
                    f"Full incident context:\n{context}\n\nEvaluation target — Cause {i}:\n{cause}"
                )
                reports.append({"name": f"cause-{i}", **self._evaluate_single(solution, expectation)})
            scores = [report["accuracy"] for report in reports]
            return {
                "success": all(report["success"] is True for report in reports),
                "accuracy": round(sum(scores) / len(scores), 2) if all(s is not None for s in scores) else None,
                "oracles": reports,
            }
        return self._evaluate_single(solution, self.expected)

    def _evaluate_single(self, solution, expectation: str) -> dict:
        print("== LLM-as-a-Judge Evaluation ==")
        results = {}

        # Normalize solution to string
        if not isinstance(solution, str):
            solution = str(solution)

        try:
            # Get detailed judgment from DiagnosisJudge using root-cause-only ground truth
            report = self.judge.judge_detailed(
                solution=solution,
                expectation=expectation,
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
            is_correct = report.verdict == JudgmentResult.TRUE

            if is_correct:
                print(f"✅ Correct diagnosis: {report.verdict.value} (score: {acc:.1f}/100)")
            else:
                print(f"❌ Incorrect diagnosis: {report.verdict.value} (score: {acc:.1f}/100)")
                print(
                    f"   Expected: {expectation[:100]}..." if len(expectation) > 100 else f"   Expected: {expectation}"
                )
                print(f"   Got: {solution[:100]}..." if len(solution) > 100 else f"   Got: {solution}")

            # Include dimension breakdown in results
            results["judgment"] = report.verdict.value
            results["reasoning"] = report.reasoning
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
