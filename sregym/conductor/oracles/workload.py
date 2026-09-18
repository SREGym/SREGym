from sregym.conductor.oracles.base import Oracle
from sregym.conductor.oracles.failure import FailureClass


def truncate(text: str, length: int = 100) -> str:
    """Truncate text to a specified length, adding ellipsis if truncated."""
    if len(text) > length:
        text = text[:length] + "..."
    text = text.encode("unicode_escape").decode("utf-8")
    return text


class WorkloadOracle(Oracle):
    importance = 3.0

    # Both failures here are honestly undecidable, which is why neither is
    # mapped to ``AGENT_ERROR``:
    #
    # - ``workload_requests_failing`` observes a *symptom*. Unlike AlertOracle,
    #   this oracle captures no pre-fault baseline, so it cannot tell a request
    #   failing because the agent left the fault in place from one failing
    #   because of unrelated cluster degradation. Giving it a baseline (as
    #   AlertOracle got in SREGym#745) is what would make this decisive.
    # - ``workload_collection_failed`` means the load generator itself did not
    #   report. That may be our wrk tooling or an evicted generator pod; the
    #   environment-health precondition is what would separate them.
    #
    # Both being non-agent still does the important work: with ``importance``
    # 3.0 this oracle dominates the compound accuracy of the five problems that
    # use it, so a bare failure here was previously the loudest way to score a
    # model down for something it may not have done.
    FAILURE_CLASSES = {
        "workload_requests_failing": FailureClass.AMBIGUOUS,
        "workload_collection_failed": FailureClass.AMBIGUOUS,
    }

    def __init__(self, problem, wrk_manager=None):
        super().__init__(problem)
        self.wrk = wrk_manager

    def evaluate(self) -> dict:
        try:
            self.wrk.collect(number=1)
            entries = self.wrk.collect(number=50)
            for entry in entries:
                if not entry.ok:
                    print(f"[❌] Workload entry at {entry.time} failed with log: {truncate(entry.log, 100)}")
                    return self.fail(
                        "workload_requests_failing",
                        entry_time=str(entry.time),
                        log=truncate(entry.log, 100),
                    )
            print(f"[✅] Successfully collected {len(entries)} workload entries.")
            return {
                "success": True,
            }
        except Exception as e:
            print(f"[❌] Error during workload collection: {e}")
            return self.fail("workload_collection_failed", error=f"{type(e).__name__}: {e}")
