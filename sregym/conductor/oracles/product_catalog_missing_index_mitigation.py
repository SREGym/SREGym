"""Mitigation oracle for the product-reviews missing-index fault"""

import logging

from sregym.conductor.oracles.failure import FailureClass
from sregym.conductor.oracles.mitigation import MitigationOracle

logger = logging.getLogger(__name__)


class ProductReviewsMissingIndexMitigationOracle(MitigationOracle):
    """
    Mitigation oracle for the product-reviews missing-index problem.

    A mitigation only passes if all of the following hold:

      1. The application is healthy: no deployment deleted, scaled to 0, or left
         with unready pods (inherited from MitigationOracle).
      2. The review history seeded at injection is still there, row for row and
         unchanged (count + content checksum over its id range). Deleting,
         truncating or archiving the history makes the scans cheap without fixing
         anything, and a postgres restart wipes it (the pod has no volume).
      3. The original reviews are still there and unchanged.
      4. For every catalog product and one discontinued product, both
         product-reviews queries, run from the product-reviews pod over the app's
         own DB_CONNECTION_STRING under EXPLAIN (ANALYZE, BUFFERS), touch only a
         small fraction of the pages the table had at injection.

         A real index lookup reads a handful of pages, while a BRIN index, a btree
         with product_id second, per-role or connection-option planner settings,
         or the app repointed at another copy still read most of the table.

         A partial index over today's catalog ids fails on the discontinued product.
         The app must still be reading the database the history checks read
         (same server and database name). Any real fix passes, whatever the index
         name or type, and so does partitioning by product_id. No row threshold.
      5. The frontend still serves the probe product and its original reviews.
    """

    # A lookup may touch at most 1/20 of the heap's pages at injection (280 of 5,594 here);
    # a real index lookup touches about 4, the fault the whole table. The floor is for tiny tables.
    LOOKUP_BUFFER_FRACTION = 20
    MIN_LOOKUP_BUFFERS = 64

    FAILURE_CLASSES = {
        # Rows only disappear from a table when someone deletes them, or when the
        # postgres pod is replaced or its container restarted (no volume, init.sql
        # re-seeds).
        "review_history_deleted": FailureClass.AGENT_ERROR,
        "review_history_modified": FailureClass.AGENT_ERROR,
        "review_history_lost_postgres_replaced": FailureClass.AGENT_ERROR,
        "review_history_lost_postgres_restarted": FailureClass.AGENT_ERROR,
        "review_table_missing": FailureClass.AGENT_ERROR,
        # Only a spec or env edit repoints the app's database connection
        "review_app_database_changed": FailureClass.AGENT_ERROR,
        "original_reviews_not_preserved": FailureClass.AGENT_ERROR,
        # An OOM kill wipes the data too. The raised limit makes it unlikely, but an
        # agent's heavy queries could also cause it, so it can't be pinned on either.
        "review_history_lost_postgres_oom": FailureClass.AMBIGUOUS,
        # Rows are gone but the postgres pod couldn't be compared with the one at injection
        "review_history_lost_unattributed": FailureClass.AMBIGUOUS,
        # Postgres didn't answer (restarting, connection refused, ...).
        "review_table_unreadable": FailureClass.AMBIGUOUS,
    }

    def evaluate(self) -> dict:
        print("--- Mitigation Evaluation (product-reviews missing index) ---")

        base = super().evaluate()
        if not base.get("success"):
            return base

        try:
            failure = self._database_failure(self.problem)
        except RuntimeError as exc:
            return self._unreadable(exc)
        if failure is not None:
            return failure

        # The app must still serve the product and its reviews.
        if not self.problem.review_reads_work():
            return self.fail(
                "review_frontend_read_failed",
                message=f"The frontend did not return product {self.problem.PROBE_PRODUCT_ID} and its original reviews",
            )

        logger.info(
            "App healthy, review history and original reviews intact, every product-reviews lookup is a real "
            "index lookup, and the frontend serves reviews. Mitigation accepted!"
        )
        return {"success": True}

    def _database_failure(self, p) -> dict | None:
        """Raises RuntimeError when the database can't be read"""
        # The review history must survive.
        rows, checksum = p.history_fingerprint()
        if rows != p.history_rows:
            code, cause = self._history_loss(p, rows)
            reason = f"{p.history_rows - rows} of {p.history_rows} history reviews are gone ({cause})"
            logger.info(reason)
            return self.fail(code, message=reason, expected=p.history_rows, found=rows)
        if checksum != p.history_checksum:
            reason = "The history reviews are all present but their contents were changed"
            logger.info(reason)
            return self.fail("review_history_modified", message=reason)

        # The original reviews must survive too.
        intact, detail = p.original_reviews_intact()
        if not intact:
            reason = f"The original reviews were not preserved: {detail}"
            logger.info(reason)
            return self.fail("original_reviews_not_preserved", message=reason)

        # Every lookup the app runs must be a real index lookup again, on the same database
        # the checks above read (not an app repointed at a trimmed copy).
        target, costs = p.lookup_costs()
        if target != p.database_target():
            reason = (
                f"product-reviews now reads a different database ({target}) than the one with the review "
                f"history ({p.database_target()})"
            )
            logger.info(reason)
            return self.fail("review_app_database_changed", message=reason)
        limit = max(self.MIN_LOOKUP_BUFFERS, p.heap_pages_at_inject // self.LOOKUP_BUFFER_FRACTION)
        heavy = [sql for sql, cost in costs.items() if cost["buffers"] > limit]
        if heavy:
            worst = max(heavy, key=lambda sql: costs[sql]["buffers"])
            reason = (
                f"{len(heavy)} of {len(costs)} product-reviews lookups still read up to {costs[worst]['buffers']} "
                f"pages of reviews.productreviews ({p.heap_pages_at_inject} at injection, limit {limit}), "
                f"e.g. {worst!r} with plan nodes {costs[worst]['nodes']}"
            )
            logger.info(reason)
            return self.fail("fault_still_present", message=reason)
        return None

    def _unreadable(self, exc: RuntimeError) -> dict:
        text = str(exc)
        if "does not exist" in text and ("relation" in text or "schema" in text):
            reason = f"reviews.productreviews is gone (dropped or renamed): {text[:300]}"
            logger.info(reason)
            return self.fail("review_table_missing", message=reason)
        reason = f"Could not read reviews.productreviews: {text[:300]}"
        logger.info(reason)
        return self.fail("review_table_unreadable", message=reason)

    @staticmethod
    def _history_loss(p, rows: int) -> tuple[str, str]:
        """Compare the postgres pod against the one seen at injection"""
        before, now = p.postgres_identity, p.current_postgres_identity()
        if not before or not now:
            return (
                "review_history_lost_unattributed",
                "the postgres pod could not be compared with the one at injection",
            )
        if now["uid"] != before["uid"]:
            return "review_history_lost_postgres_replaced", "the postgres pod was replaced, which re-seeds the DB"
        if now["restarts"] > before["restarts"]:
            if now["last_reason"] == "OOMKilled":
                return "review_history_lost_postgres_oom", "postgres was OOM-killed, which re-seeds the DB"
            return "review_history_lost_postgres_restarted", "postgres restarted, which re-seeds the DB"
        return "review_history_deleted", f"{rows} left in the seeded id range"
