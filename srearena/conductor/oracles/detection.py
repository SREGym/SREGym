from srearena.conductor.oracles.base import Oracle
from srearena.conductor.oracles.utils import is_exact_match


class DetectionOracle(Oracle):
    def __init__(self, problem, expected="Yes"):
        super().__init__(problem)
        self.expected = expected

    def evaluate(self, solution) -> dict:
        print("== Detection Evaluation ==")
        results = {}

        if isinstance(solution, str):
            is_correct = is_exact_match(solution.strip().lower(), self.expected.lower())
            results["Detection Accuracy"] = "Correct" if is_correct else "Incorrect"
            results["success"] = is_correct
            print(f"{'✅' if is_correct else '❌'} Detection: {solution}")
        else:
            results["Detection Accuracy"] = "Invalid Format"
            results["success"] = False
            print("❌ Invalid detection format")

        return results
