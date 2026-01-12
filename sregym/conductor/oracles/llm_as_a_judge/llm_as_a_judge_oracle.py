"""LLM-as-a-Judge Oracle for evaluating agent solutions using LLM judgment."""

from typing import Optional

from sregym.conductor.oracles.base import Oracle
from sregym.conductor.oracles.llm_as_a_judge.judge import JudgmentResult, LLMJudge


class LLMAsAJudgeOracle(Oracle):
    """Oracle that uses an LLM judge to evaluate agent solutions against expected root causes."""

    def __init__(
        self,
        problem,
        expected: str,
        provider: Optional[str] = None,
        model_name: Optional[str] = None,
        url: Optional[str] = None,
        api_key: Optional[str] = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
    ):
        super().__init__(problem)
        self.expected = expected if expected else ""

        # Initialize the LLM judge
        self.judge = LLMJudge(
            provider=provider,
            model_name=model_name,
            url=url,
            api_key=api_key,
            temperature=temperature,
            max_tokens=max_tokens,
        )

    def evaluate(self, solution) -> dict:
        print("== LLM-as-a-Judge Evaluation ==")
        results = {}

        # Normalize solution to string
        if not isinstance(solution, str):
            solution = str(solution)

        try:
            # Get judgment from LLM judge
            judgment, reasoning = self.judge.judge(solution=solution, expectation=self.expected)

            # Determine success based on judgment
            is_correct = judgment == JudgmentResult.TRUE

            if is_correct:
                acc = 100.0
                print(f"✅ Correct diagnosis: {judgment.value}")
            else:
                acc = 0.0
                print(f"❌ Incorrect diagnosis: {judgment.value}")
                print(
                    f"   Expected: {self.expected[:100]}..."
                    if len(self.expected) > 100
                    else f"   Expected: {self.expected}"
                )
                print(f"   Got: {solution[:100]}..." if len(solution) > 100 else f"   Got: {solution}")

            results["judgment"] = judgment.value
            results["reasoning"] = reasoning
            results["success"] = is_correct
            results["accuracy"] = acc

        except Exception as e:
            print(f"❌ Error during LLM judgment: {e}")
            results["judgment"] = "Error"
            results["reasoning"] = f"Error: {str(e)}"
            results["success"] = False
            results["accuracy"] = 0.0
            results["error"] = str(e)

        return results
