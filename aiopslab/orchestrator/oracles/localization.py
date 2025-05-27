# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Localization Oracle for evaluating fault localization accuracy."""

from aiopslab.orchestrator.evaluators.quantitative import is_exact_match, is_subset
from aiopslab.orchestrator.oracles.base import Oracle


class LocalizationOracle(Oracle):
    def __init__(self, problem, expected="user-service"):
        super().__init__(problem)
        self.expected = expected

    def evaluate(self, solution, trace, duration) -> dict:
        print("== Localization Evaluation ==")

        if solution is None:
            print("❌ Solution is None")
            self.problem.add_result("Localization Accuracy", 0.0)
            self.problem.results["success"] = False
            self.problem.results["is_subset"] = False
            return self.problem.eval(solution, trace, duration)

        is_exact = is_exact_match(solution, self.expected)
        is_sub = is_subset([self.expected], solution)

        if is_exact:
            acc = 100.0
            print(f"✅ Exact match: {solution}")
        elif is_sub:
            acc = (1 / len(solution)) * 100.0
            print(f"⚠️ Subset match: {solution} | Accuracy: {acc:.2f}%")
        else:
            acc = 0.0
            print(f"❌ No match: {solution}")

        self.problem.add_result("Localization Accuracy", acc)
        self.problem.results["success"] = is_exact or (is_sub and len(solution) == 1)
        self.problem.results["is_subset"] = is_sub

        return self.problem.eval(solution, trace, duration)
