from sregym.conductor.oracles.base import Oracle
from sregym.conductor.oracles.utils import is_exact_match, is_subset


class LocalizationOracle(Oracle):
    def __init__(self, problem, expected: list[str] | str):
        super().__init__(problem)
        # Normalize expected to list of strings
        if isinstance(expected, str):
            self.expected = [expected]
        elif isinstance(expected, list):
            # Flatten if nested and ensure all items are strings
            flattened = []
            for item in expected:
                if isinstance(item, list):
                    flattened.extend([str(x) for x in item])
                else:
                    flattened.append(str(item))
            self.expected = flattened
        else:
            self.expected = [str(expected)]

    def evaluate(self, solution) -> dict:
        print("== Localization Evaluation ==")
        results = {}

        # Normalize solution to list of strings
        if isinstance(solution, str):
            # Check if it's a comma-separated list
            if "," in solution:
                solution = [s.strip() for s in solution.split(",")]
            else:
                solution = [solution]
        elif isinstance(solution, list):
            # Ensure all items are strings
            solution = [str(item) for item in solution]
        else:
            results["accuracy"] = 0.0
            results["success"] = False
            results["is_subset"] = False
            print("❌ Invalid format: expected string or list of strings")
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

        results["accuracy"] = acc
        results["success"] = is_exact or (is_sub and len(solution) == len(self.expected))
        results["is_subset"] = is_sub

        return results
