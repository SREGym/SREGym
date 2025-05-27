"""Detection Oracle for evaluating detection accuracy."""

from aiopslab.orchestrator.evaluators.quantitative import is_exact_match
from aiopslab.orchestrator.oracles.base import Oracle


class DetectionOracle(Oracle):
    def __init__(self, problem, expected="Yes"):
        super().__init__(problem)
        self.expected = expected

    def evaluate(self, solution, trace, duration) -> dict:
        print("== Detection Evaluation ==")

        if isinstance(solution, str):
            if is_exact_match(solution.strip().lower(), self.expected.lower()):
                print(f"✅ Correct detection: {solution}")
                self.problem.add_result("Detection Accuracy", "Correct")
                self.problem.results["success"] = True
            else:
                print(f"❌ Incorrect detection: {solution}")
                self.problem.add_result("Detection Accuracy", "Incorrect")
                self.problem.results["success"] = False
        else:
            print("❌ Invalid detection format")
            self.problem.add_result("Detection Accuracy", "Invalid Format")
            self.problem.results["success"] = False

        return self.problem.eval(solution, trace, duration)
