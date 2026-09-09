import logging

from sregym.conductor.oracles.mitigation import MitigationOracle

logger = logging.getLogger(__name__)


class IntegerOverflowPrimaryKeyMitigationOracle(MitigationOracle):
    """
    Mitigation oracle for the integer-overflow primary-key problem:

    The default pod-health oracle isn't enough here because the application can
    keep running even after the ID sequence is exhausted. This oracle adds a few
    database checks to make sure the fix actually solves the problem.

    A mitigation only passes if all of the following are true:

      1. The application stays healthy -> no deployment deleted, scaled to 0, or
         left with unready pods (inherited from MitigationOracle).
      2. The original seeded review data is intact (no TRUNCATE or DROP).
      3. New writes to reviews.productreviews succeed again.
      4. The ID sequence has plenty of headroom left (the durable BIGINT migration,
         instead of a temporary one-ID sequence reset that re-exhausts immediately).
    """

    # A durable fix leaves plenty of ids.
    MIN_ID_HEADROOM = 2147483647 // 2  # 1.07B ids
    # init.sql seeds 50 reviews, so a valid fix must not delete them.
    EXPECTED_MIN_ROWS = 50

    def evaluate(self) -> dict:
        print("--- Mitigation Evaluation (product-reviews integer overflow) ---")

        # 1. Application health first: reject "fixes" that repair the database but
        #    break the service (deployment deleted, scaled to 0, or pods not ready)
        base = super().evaluate()
        if not base.get("success"):
            return base

        # 2. The fix must not have destroyed the original review data
        #    (e.g. 'TRUNCATE reviews.productreviews RESTART IDENTITY' would
        #    clear the sequence AND the rows)
        row_count = self.problem._review_row_count()
        if row_count < self.EXPECTED_MIN_ROWS:
            reason = (
                f"reviews.productreviews has only {row_count} rows "
                f"(expected >= {self.EXPECTED_MIN_ROWS}). The fix destroyed existing review data."
            )
            logger.info(reason)
            return {"success": False, "reason": reason}

        # 3. Writes must succeed again
        status = self.problem._review_write_status()
        if status != "ok":
            reason = (
                f"A write to reviews.productreviews did not succeed (status={status}); "
                "the id sequence is still exhausted."
            )
            logger.info(reason)
            return {"success": False, "reason": reason}

        # 4. The durable BIGINT migration
        headroom = self.problem._review_id_headroom()
        if headroom < self.MIN_ID_HEADROOM:
            reason = (
                f"Writes succeed but the id sequence has only {headroom} ids left "
                f"(require >= {self.MIN_ID_HEADROOM}); widen the id column and sequence to "
                "BIGINT rather than resetting the sequence."
            )
            logger.info(reason)
            return {"success": False, "reason": reason}

        logger.info(
            "App is healthy, review data is intact, writes are working again, and the ID sequence has plenty of headroom. Mitigation accepted!"
        )
        return {"success": True}
