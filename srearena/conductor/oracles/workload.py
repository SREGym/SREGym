from srearena.conductor.oracles.base import Oracle


def truncate(text: str, length: int = 100) -> str:
    """Truncate text to a specified length, adding ellipsis if truncated."""
    if len(text) > length:
        return text[:length] + "..."
    return text


class WorkloadOracle(Oracle):
    def __init__(self, problem, wrk_manager=None):
        super().__init__(problem)
        self.wrk = wrk_manager

    def evaluate(self) -> dict:
        try:
            entries = self.wrk.collect(number=50)
            for entry in entries:
                if not entry.ok:
                    print(f"[❌] Workload entry at {entry.time} failed with log: {truncate(entry.log, 100)}")
                    return {
                        "success": False,
                    }
            print(f"[✅] Successfully collected {len(entries)} workload entries.")
            return {
                "success": True,
            }
        except Exception as e:
            print(f"[❌] Error during workload collection: {e}")
            return {
                "success": False,
            }
