import logging

from sregym.conductor.oracles.base import Oracle
from sregym.conductor.oracles.failure import FailureClass
from sregym.conductor.oracles.utils import is_exact_match

logger = logging.getLogger("all.sregym.oracle")
logger.propagate = True
logger.setLevel(logging.DEBUG)


class DetectionOracle(Oracle):
    # Both failures are the agent's: it either answered the detection question
    # wrongly or did not answer in the required form. Neither depends on the
    # cluster.
    FAILURE_CLASSES = {
        "detection_incorrect": FailureClass.AGENT_ERROR,
        "invalid_solution_format": FailureClass.AGENT_ERROR,
    }

    def __init__(self, problem):
        super().__init__(problem)

    def evaluate(self, solution) -> dict:
        expected = "Yes" if self.problem.fault_injected else "No"
        logger.info(f"== Detection Evaluation (expected: {expected}) ==")

        if isinstance(solution, str):
            is_correct = is_exact_match(solution.strip().lower(), expected.lower())
            logger.info(f"{'✅' if is_correct else '❌'} Detection: {solution}")
            if is_correct:
                return {"accuracy": 100.0, "success": True}
            return {"accuracy": 0.0, **self.fail("detection_incorrect", expected=expected, answered=solution.strip())}

        logger.warning("❌ Invalid detection format")
        # This oracle already returned a ``reason``, but as the free-text
        # "Invalid Format" -- not a code anything could filter or aggregate on.
        # Renaming it to the shared snake_case form is the point of the sweep.
        return {"accuracy": 0.0, **self.fail("invalid_solution_format", got_type=type(solution).__name__)}
