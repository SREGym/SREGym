"""LLM-as-a-Judge Oracle for evaluating agent solutions using LLM judgment."""

from sregym.conductor.oracles.base import Oracle
from sregym.conductor.oracles.llm_as_a_judge.jev_as_a_judge import JevAsAJudge, JevVerdict, judge_mode
from sregym.conductor.oracles.llm_as_a_judge.judge import DiagnosisJudge, JudgmentResult


class LLMAsAJudgeOracle(Oracle):
    """Oracle that judges agent diagnoses against expected root causes.

    ``JUDGE_MODE`` (or ``mode``) picks the judge: ``llm`` (default) asks the LLM checklist judge; ``jev`` asks Jev
    alone; ``mixed`` asks Jev first and the LLM judge only for the reports Jev is unsure about (``jev_as_a_judge.py``).
    """

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
        mode: str | None = None,
        jev_as_a_judge: JevAsAJudge | None = None,
    ):
        super().__init__(problem)
        self.expected = expected if expected else ""
        self.mode = mode or judge_mode()
        self.jev = jev_as_a_judge or (JevAsAJudge() if self.mode != "llm" else None)

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
        # Normalize solution to string
        if not isinstance(solution, str):
            solution = str(solution)

        if self.mode == "llm":
            return self._evaluate_llm(solution)

        print(f"== Jev-as-a-Judge Evaluation ({self.mode}) ==")
        try:
            verdict = self.jev.judge(solution, self.expected, self.mode)
        except Exception as e:
            print(f"❌ Error during Jev judgment: {e}")
            if self.mode == "jev":
                return {
                    "judgment": "Error",
                    "reasoning": f"Error: {str(e)}",
                    "success": False,
                    "accuracy": 0.0,
                    "checklist": [],
                    "error": str(e),
                    "jev": {"mode": self.mode, "model": self.jev.model, "error": str(e)},
                }
            print("   Falling back to the LLM judge")
            results = self._evaluate_llm(solution)
            results["jev"] = {"mode": self.mode, "model": self.jev.model, "error": str(e), "sent_to_llm": True}
            return results

        info = self._jev_info(verdict)
        if verdict.send_to_llm:
            print(f"   Jev score {verdict.score:.3f}: unsure, asking the LLM judge")
            results = self._evaluate_llm(solution)
            results["jev"] = info
            return results
        return self._jev_results(verdict, info)

    def _jev_info(self, verdict: JevVerdict) -> dict:
        """What Jev answered and whether the report went to the LLM judge, kept under ``results["jev"]``."""
        return {
            "mode": self.mode,
            "model": self.jev.model,
            "score": verdict.score,
            "p": verdict.p,
            "sent_to_llm": verdict.send_to_llm,
        }

    def _jev_results(self, verdict: JevVerdict, info: dict) -> dict:
        """Results in the LLM judge's shape, from Jev's verdict alone."""
        acc = round(verdict.score * 100.0, 2)
        mark = "✅ Correct" if verdict.passed else "❌ Incorrect"
        print(f"{mark} diagnosis (Jev): {verdict.passed} (score: {acc:.1f}/100)")
        if self.mode == "jev":
            rule = f"score >= {self.jev.config['cut_jev_alone']}"
        else:
            rule = f"score outside [{self.jev.config['band_low']}, {self.jev.config['band_high']}]"
        outcome = "pass" if verdict.passed else "fail"
        return {
            "judgment": (JudgmentResult.TRUE if verdict.passed else JudgmentResult.FALSE).value,
            "reasoning": f"Jev ({self.jev.model}) score {verdict.score:.3f}, {rule}: {outcome}.",
            "success": bool(verdict.passed),
            "accuracy": acc,
            "composite_score": verdict.score,
            "dimensions": {did: {"name": did, "score": round(v, 4)} for did, v in verdict.dimensions.items()},
            # Jev gives P(yes) per question; the answer shown is the likelier one, the score uses P(yes) itself
            "checklist": [
                {"id": qid, "answer": "Yes" if p >= 0.5 else "No", "evidence": "", "confidence": f"P(yes)={p:.3f}"}
                for qid, p in verdict.p.items()
            ],
            "jev": info,
        }

    def _evaluate_llm(self, solution: str) -> dict:
        """Judge the diagnosis with the LLM checklist judge (DiagnosisJudge)."""
        print("== LLM-as-a-Judge Evaluation ==")
        results = {}

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
            is_correct = report.verdict == JudgmentResult.TRUE

            if is_correct:
                print(f"✅ Correct diagnosis: {report.verdict.value} (score: {acc:.1f}/100)")
            else:
                print(f"❌ Incorrect diagnosis: {report.verdict.value} (score: {acc:.1f}/100)")
                print(
                    f"   Expected: {self.expected[:100]}..."
                    if len(self.expected) > 100
                    else f"   Expected: {self.expected}"
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
