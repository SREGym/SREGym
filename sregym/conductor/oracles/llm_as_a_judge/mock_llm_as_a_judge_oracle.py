"""Mock LLM-as-a-Judge Oracle for demo purposes."""

from typing import Optional

from sregym.conductor.oracles.base import Oracle
from sregym.conductor.oracles.llm_as_a_judge.judge import JudgmentResult, LLMJudge

class MockLLMJudge:
    def __init__(self, ground_truth):
        self.ground_truth = ground_truth

    def judge(self, solution):
        if solution == self.ground_truth:
            return JudgmentResult.TRUE, "The agent provided a correct diagnosis."
        return JudgmentResult.FALSE, "The agent provided an incorrect diagnosis."


class MockLLMAsAJudgeOracle(Oracle):
    """(DEMO ONLY) Oracle that verifies if provided string is exactly the root cause of the problem."""

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
        self.judge = MockLLMJudge(problem.root_cause)

    def evaluate(self, solution) -> dict:
        print("== Mock LLM-as-a-Judge Evaluation ==")
        results = {}

        # Normalize solution to string
        if not isinstance(solution, str):
            solution = str(solution)

        try:
            # Get judgment from LLM judge
            judgment, reasoning = self.judge.judge(solution=solution)

            # Check if judge is not initialized
            if judgment is None:
                print("⚠️  LLM judge is not initialized - returning null result")
                results["judgment"] = None
                results["reasoning"] = reasoning
                results["success"] = None
                results["accuracy"] = None
                return results

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
