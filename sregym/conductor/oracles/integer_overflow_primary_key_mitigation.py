import logging

from sregym.conductor.oracles.failure import FailureClass
from sregym.conductor.oracles.mitigation import MitigationOracle

logger = logging.getLogger(__name__)


class IntegerOverflowPrimaryKeyMitigationOracle(MitigationOracle):
    """
    Mitigation oracle for the integer-overflow primary-key problem.

    The default pod-health oracle isn't enough here because product-reviews keeps
    running even after the ID sequence is exhausted. This oracle adds a set of
    database checks to make sure the mitigation actually fixes the problem, rather
    than just making the symptoms disappear.

    A mitigation only passes if all of the following are true:

      1. The application stays healthy -> no deployment deleted, scaled to 0, or
         left with unready pods (inherited from MitigationOracle).
      2. The original seeded review data is still intact. Deleting the data and
         recreating enough rows to match the original count is rejected (verified
         using the original primary keys and a content signature captured during
         fault injection).
      3. The marker review inserted during fault injection is still present. This
         catches database re-seeds (for example, restarting the postgres pod) as
         well as TRUNCATE or DROP, which restore the data but not the marker row.
      4. The `id` column is still protected by a PRIMARY KEY, UNIQUE constraint,
         or equivalent unique index. Simply dropping protection so duplicate IDs
         are accepted is not considered as a valid fix.
      5. A review can be inserted as the application user (`otelu`). Fixing the
         sequence as a superuser isn't enough if the application's normal write path
         is still broken (for example, if INSERT privileges were removed).
      6. The ID sequence has plenty of safe headroom, doesn't cycle, can't generate
         values outside the column's type, and won't hand out IDs that already exist.
         This accepts a proper BIGINT migration (or other genuinely durable fixes,
         like descending sequence using the unused negative range) while rejecting
         quick fixes like resetting the sequence, widening only the sequence while
         leaving the column as INTEGER, or any repair that would eventually collide
         with existing IDs. The sequence is resolved dynamically using
         `pg_get_serial_sequence`, so renaming it after a valid repair still passes.

    """

    # A durable fix leaves plenty of ids.
    MIN_ID_HEADROOM = 2147483647 // 2  # 1.07B

    FAILURE_CLASSES = {
        "review_sequence_exhausted": FailureClass.AGENT_ERROR,
        "review_write_denied": FailureClass.AGENT_ERROR,
        "review_id_collision": FailureClass.AGENT_ERROR,
        "review_sequence_type_mismatch": FailureClass.AGENT_ERROR,
        "review_sequence_cycles": FailureClass.AGENT_ERROR,
        "review_sequence_collision_risk": FailureClass.AGENT_ERROR,
        "review_sequence_capacity_low": FailureClass.AGENT_ERROR,
    }

    def evaluate(self) -> dict:
        print("--- Mitigation Evaluation (product-reviews integer overflow) ---")
        p = self.problem

        # 1. Application health first: reject "fixes" that repair the database but
        #    break the service (deployment deleted, scaled to 0, or pods not ready).
        base = super().evaluate()
        if not base.get("success"):
            return base

        # 2. The fix must not have destroyed the original review data
        intact, detail = p._original_reviews_intact()
        if not intact:
            reason = f"The original seeded reviews were not preserved: {detail}."
            logger.info(reason)
            return self.fail("review_data_not_preserved", message=reason)

        # 3. The injected marker review must still be present. Its absence means the
        #    data was either re-seeded or wiped (postgres pod restart / TRUNCATE / DROP).
        if not p._review_sentinel_present():
            reason = (
                "The injected marker review is missing. The database was re-seeded or "
                "wiped (e.g. a postgres pod restart or TRUNCATE), which is not a valid mitigation"
            )
            logger.info(reason)
            return self.fail("preserved_review_missing", message=reason)

        # 4. id must still be uniqueness-protected. Dropping the primary key so
        #    overflowing/duplicate ids are accepted, destroys review-id uniqueness.
        if not p._id_uniqueness_enforced():
            reason = (
                "reviews.productreviews.id no longer has unconditional unique protection, "
                "so duplicate ids are accepted. Uniqueness must be preserved"
            )
            logger.info(reason)
            return self.fail("review_id_uniqueness_missing", message=reason)

        # 5. Writes must succeed AS THE APPLICATION USER, not just the superuser.
        status = p._review_write_status()
        if status != "ok":
            cause = {
                "exhausted": "the id sequence is still exhausted",
                "denied": "the application user (otelu) can no longer INSERT; the write path is still broken",
                "collision": "writes collide with existing ids (the sequence hands out ids that are already used)",
            }.get(status, f"a write by the application user did not succeed (status={status})")

            reason = f"A review write did not succeed: {cause}"
            logger.info(reason)
            code = {
                "exhausted": "review_sequence_exhausted",
                "denied": "review_write_denied",
                "collision": "review_id_collision",
            }.get(status, "review_write_failed")
            return self.fail(code, message=reason)

        # 6. The sequence must have real, collision-free headroom and not cycle.
        cap = p._id_sequence_capacity()
        if cap is None:
            reason = "Could not resolve the identity sequence backing reviews.productreviews.id"
            logger.info(reason)
            return self.fail("review_sequence_unavailable", message=reason)

        if not cap["fits_column"]:
            reason = (
                "The id sequence can generate values beyond the id column's type range, so the new ids "
                "overflow the column and make inserts fail (e.g. widening the sequence to "
                "BIGINT without widening the id column). Widen the id column to match the sequence"
            )
            logger.info(reason)
            return self.fail("review_sequence_type_mismatch", message=reason)

        if cap["cycle"]:
            reason = "The id sequence is set to CYCLE, so ids will eventually be reused"
            logger.info(reason)
            return self.fail("review_sequence_cycles", message=reason)

        if not cap["collision_free"]:
            reason = (
                "The id sequence is positioned to hand out ids that already exist "
                "(a reset toward occupied ids), which will collide on the next inserts"
            )
            logger.info(reason)
            return self.fail("review_sequence_collision_risk", message=reason)

        if cap["headroom"] < self.MIN_ID_HEADROOM:
            reason = (
                f"Writes succeed but the id sequence has only {cap['headroom']} collision-free ids "
                f"left (require >= {self.MIN_ID_HEADROOM}); widen the id column and sequence to BIGINT "
                "rather than resetting the sequence."
            )
            logger.info(reason)
            return self.fail("review_sequence_capacity_low", message=reason)

        if not p._review_reads_work():
            return self.fail(
                "review_frontend_read_failed", message="The frontend could not retrieve the preserved product review."
            )

        logger.info(
            "App is healthy, original reviews intact, marker present, id uniqueness enforced, "
            "application-user writes are working again, and the sequence has durable collision-free headroom"
            "Mitigation accepted!"
        )
        return {"success": True}
