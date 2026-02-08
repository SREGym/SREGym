import logging

from sregym.conductor.oracles.base import Oracle
from sregym.conductor.oracles.utils import is_exact_match

logger = logging.getLogger("all.sregym.oracle")
logger.propagate = True
logger.setLevel(logging.DEBUG)


class DetectionOracle(Oracle):
    def __init__(self, problem):
        super().__init__(problem)

    def evaluate(self, solution) -> dict:
        expected = "Yes" if self.problem.fault_injected else "No"
        logger.info(f"== Detection Evaluation (expected: {expected}) ==")

        results = {}
        if isinstance(solution, str):
            is_correct = is_exact_match(solution.strip().lower(), expected.lower())
            results["accuracy"] = 100.0 if is_correct else 0.0
            results["success"] = is_correct
            logger.info(f"{'✅' if is_correct else '❌'} Detection: {solution}")
        else:
            results["accuracy"] = 0.0
            results["success"] = False
            results["reason"] = "Invalid Format"
            logger.warning("❌ Invalid detection format")

        return results
