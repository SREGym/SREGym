"""Data-Plane Progress Oracle (DPPO)."""

import re
import time

from sregym.conductor.oracles.base import Oracle


class DataPlaneProgressOracle(Oracle):
    importance = 1.0

    def __init__(
        self,
        problem,
        consumer_group: str,
        topic: str,
        consumer_deployment: str,
        archiver_deployment: str = "orders-archiver",
        settle_seconds: int = 30,
        progress_timeout: int = 240,
        progress_window_seconds: int = 45,
        restart_recover_timeout: int = 300,
    ):
        super().__init__(problem)
        self.consumer_group = consumer_group
        self.topic = topic
        self.consumer_deployment = consumer_deployment
        self.archiver_deployment = archiver_deployment
        self.settle_seconds = settle_seconds
        self.progress_timeout = progress_timeout
        self.progress_window_seconds = progress_window_seconds
        self.restart_recover_timeout = restart_recover_timeout

    def _consumer_pods(self):
        pods = self.problem.kubectl.list_pods(self.problem.namespace)
        return [pod for pod in pods.items if (pod.metadata.labels or {}).get("app") == self.consumer_deployment]

    def _running_consumer_pods(self):
        return [pod for pod in self._consumer_pods() if pod.status.phase == "Running"]

    def _processed_offsets(self) -> set[int]:
        logs = self.problem.kubectl.exec_command(
            f"kubectl logs deployment/{self.archiver_deployment} -n {self.problem.namespace} --tail=20000"
        )
        return {int(x) for x in re.findall(r"AUDIT src_offset=(\d+)", logs)}

    def _max_processed(self, default: int = -1) -> int:
        processed = self._processed_offsets()
        return max(processed) if processed else default

    def _await_progress_past(self, poison: int, timeout: int):
        deadline = time.time() + timeout
        while time.time() < deadline:
            processed = self._processed_offsets()
            if processed and max(processed) > poison:
                return processed
            time.sleep(10)
        return None

    def evaluate(self) -> dict:
        print("== Data-Plane Progress Oracle ==")
        poison = getattr(self.problem, "poison_offset", None)
        if poison is None:
            return {"success": False, "reason": "poison_offset unknown (fault not injected?)"}

        print(f"⏳ Settling {self.settle_seconds}s before evaluation...")
        time.sleep(self.settle_seconds)
        print(f"   Checking processed records advance past poison offset {poison}...")
        processed = self._await_progress_past(poison, self.progress_timeout)
        if processed is None:
            print(f"❌ No records processed past the poison offset {poison}")
            return {"success": False, "reason": "offset not advanced past poison record"}
        high = max(processed)
        print(f"   processed up to src_offset {high} ({len(processed)} records; poison was {poison})")

        missing = sorted((set(range(high + 1)) - {poison}) - processed)
        if missing:
            print(f"❌ {len(missing)} record(s) never processed, e.g. {missing[:10]} — data loss")
            return {"success": False, "reason": "data loss: valid records skipped"}

        time.sleep(self.progress_window_seconds)
        high_after = self._max_processed(default=high)
        print(f"   forward progress: {high} -> {high_after}")
        if high_after <= high:
            print("❌ No new records processed — pipeline is not making progress")
            return {"success": False, "reason": "no forward progress"}

        running = self._running_consumer_pods()
        if not running:
            return {"success": False, "reason": "consumer not Running before restart probe"}
        victim = running[0].metadata.name
        print(f"🔁 Restart-resistance probe: deleting consumer pod {victim}")
        self.problem.kubectl.exec_command(f"kubectl delete pod {victim} -n {self.problem.namespace} --wait=true")

        baseline = self._max_processed(default=high_after)
        deadline = time.time() + self.restart_recover_timeout
        while time.time() < deadline:
            if self._running_consumer_pods():
                now = self._max_processed(default=baseline)
                if now > baseline:
                    print(f"   post-restart progress: {baseline} -> {now}")
                    print("✅ Processed past the poison, no gaps, restart-resistant")
                    return {"success": True}
            time.sleep(10)
        print("❌ Pipeline did not resume processing after restart — fix is not durable")
        return {"success": False, "reason": "not restart-resistant"}
