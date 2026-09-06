import logging

from sregym.conductor.oracles.base import Oracle

logger = logging.getLogger(__name__)


class IntegerOverflowPrimaryKeyMitigationOracle(Oracle):
    """
    Mitigation oracle for the integer-overflow primary-key problem.

    It accepts the mitigation only when reviews.productreviews accepts a write again.
    The default pod-health oracle can't detect this fault since every pod stays Running while
    the id sequence is exhausted, so only an actual write reveals whether it is fixed.
    """

    def __init__(self, problem):
        super().__init__(problem)

    def evaluate(self) -> dict:
        print("--- Mitigation Evaluation (product-reviews insert) ---")

        status = self.problem._review_write_status()

        if status == "ok":
            logger.info("review.productreviews accepts write again; mitigation accepted!")
            return {"success": True}

        logger.info("reviews.productreviews write status is '%s'; not mitigated.", status)
        return {
            "success": False,
            "reason": (
                f"A write to reviews.productreviews did not succeed (status={status}); "
                "the id sequence is still exhausted."
            ),
        }
