import logging

from sregym.conductor.oracles.base import Oracle

logger = logging.getLogger(__name__)


class PostgresLockMitigationOracle(Oracle):
    """
    Mitigation oracle for the postgres lock-contention problem:

    The default MitigationOracle only checks the pod health, which is insufficient.

    Here, the fault holds a database lock, so every pod stays Running the whole
    time.

    It delegates to the problem's `_catalog_read_status()` helper, which runs a
    short-lock_timeout read of catalog.products and reports:
     - "ok" (readable),
     - "locked" (blocked by an ACCESS EXCLUSIVE lock)
     - "other" (some other failure)

    Mitigation is accepted only on "ok"
    """

    def __init__(self, problem):
        super().__init__(problem=problem)

    def evaluate(self) -> dict:
        print("--- Mitigation Evaluation (Postgres lock contention) ---")

        status = self.problem._catalog_read_status()
        if status == "ok":
            logger.info("catalog.products is readable again; mitigation accepted.")
            return {"success": True}

        logger.info("catalog.products read status is '%s'; not mitigated.", status)
        return {
            "success": False,
            "reason": (
                "A read of catalog.products did not succeed (status="
                f"{status}); an ACCESS EXCLUSIVE lock is still held on the table."
            ),
        }
