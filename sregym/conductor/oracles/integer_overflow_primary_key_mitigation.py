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
      2. The original seeded review data is still intact. We check that the row count
         hasn't changed and that a fault sentinel row is still present. This catches
         cases where the table was TRUNCATED or DROPPED, or the database was re-seeded
         after a postgres pod restart, even if writes start working again afterwards.
      4. The ID sequence has plenty of headroom left (the durable BIGINT migration,
         instead of a temporary one-ID sequence reset that re-exhausts immediately).
    """

    # A durable fix leaves plenty of ids.
    MIN_ID_HEADROOM = 2147483647 // 2  # 1.07B ids

    def evaluate(self) -> dict:
        print("--- Mitigation Evaluation (product-reviews integer overflow) ---")
        p = self.problem

        # 1. Application health first: reject "fixes" that repair the database but
        #    break the service (deployment deleted, scaled to 0, or pods not ready)
        base = super().evaluate()
        if not base.get("success"):
            return base

        # 2. The original seeded reviews must survive intact -- not deleted, not
        #    overwritten, not swapped out for fresh padding rows.
        intact, detail = p._original_reviews_intact()
        if not intact:
            reason = f"The original seeded reviews were not preserved: {detail}."
            logger.info(reason)
            return {"success": False, "reason": reason}

        # 3. The injected marker review must still be present. Its absence means the
        #    data was re-seeded or wiped (postgres pod restart / TRUNCATE / DROP).
        if not p._review_sentinel_present():
            reason = (
                "The injected marker review is missing. The database was re-seeded or "
                "wiped (e.g. a postgres pod restart or TRUNCATE), which is not a valid mitigation."
            )
            logger.info(reason)
            return {"success": False, "reason": reason}

        # 4. id must still be uniqueness-protected. Dropping the primary key so
        #    overflowing/duplicate ids are accepted destroys review-id uniqueness.
        if not p._id_uniqueness_enforced():
            reason = (
                "reviews.productreviews.id no longer has a primary key / unique constraint, "
                "so duplicate ids are accepted. Uniqueness must be preserved."
            )
            logger.info(reason)
            return {"success": False, "reason": reason}

        # 5. Writes must succeed AS THE APPLICATION USER, not just the superuser.
        status = p._review_write_status()
        if status != "ok":
            cause = {
                "exhausted": "the id sequence is still exhausted",
                "denied": "the application user (otelu) can no longer INSERT; the write path is still broken",
                "collision": "writes collide with existing ids (the sequence hands out ids that are already used)",
            }.get(status, f"a write by the application user did not succeed (status={status})")
            reason = f"A review write did not succeed: {cause}."
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
