from sregym.conductor.oracles.base import Oracle


def truncate(text: str, length: int = 100) -> str:
    """Truncate text to a specified length, adding ellipsis if truncated."""
    if len(text) > length:
        text = text[:length] + "..."
    text = text.encode("unicode_escape").decode("utf-8")
    return text


class WorkloadOracle(Oracle):
    importance = 3.0

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
