from srearena.conductor.oracles.base import Oracle
from srearena.conductor.oracles.utils import is_exact_match, is_subset


class LocalizationOracle(Oracle):
    def __init__(self, problem, expected: list[str]):
        super().__init__(problem)
        self.expected = expected

    def evaluate(self, solution) -> dict:
        print("== Localization Evaluation ==")
        results = {}

        # Normalize string input to list
        if isinstance(solution, str):
            solution = [solution]
        elif not isinstance(solution, list):
            results["Localization Accuracy"] = 0.0
            results["success"] = False
            results["is_subset"] = False
            print("❌ Invalid format: expected string or list of strings")
            return results

        # Safety check: ensure all items are strings
        if not all(isinstance(item, str) for item in solution):
            results["Localization Accuracy"] = 0.0
            results["success"] = False
            results["is_subset"] = False
            print("❌ Invalid content: all items must be strings")
            return results

        is_exact = is_exact_match(solution, self.expected)
        is_sub = is_subset(solution, self.expected)

        if is_exact:
            acc = 100.0
            print(f"✅ Exact match: {solution}")
        elif is_sub:
            acc = (len(solution) / len(self.expected)) * 100.0
            print(f"⚠️ Subset match: {solution} | Accuracy: {acc:.2f}%")
        else:
            acc = 0.0
            print(f"❌ No match: {solution}")

        results["Localization Accuracy"] = acc
        results["success"] = is_exact or (is_sub and len(solution) == len(self.expected))
        results["is_subset"] = is_sub

        return results
