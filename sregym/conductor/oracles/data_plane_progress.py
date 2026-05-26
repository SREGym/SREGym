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
        settle_seconds: int = 30,
        progress_timeout: int = 300,
        progress_window_seconds: int = 45,
        restart_recover_timeout: int = 360,
    ):
        super().__init__(problem)
        self.consumer_group = consumer_group
        self.topic = topic
        self.consumer_deployment = consumer_deployment
        self.settle_seconds = settle_seconds
        self.progress_timeout = progress_timeout
        self.progress_window_seconds = progress_window_seconds
        self.restart_recover_timeout = restart_recover_timeout

    def _consumer_pods(self):
        pods = self.problem.kubectl.list_pods(self.problem.namespace)
        return [
            pod
            for pod in pods.items
            if pod.metadata.labels and pod.metadata.labels.get("app") == self.consumer_deployment
        ]

    def _running_consumer_pods(self):
        return [pod for pod in self._consumer_pods() if pod.status.phase == "Running"]

    def _logs(self) -> str:
        return self.problem.kubectl.exec_command(
            f"kubectl logs deployment/{self.consumer_deployment} "
            f"-n {self.problem.namespace} --tail=20000"
        )

    @staticmethod
    def _committed_offsets(logs: str) -> list[int]:
        return [int(x) for x in re.findall(r"COMMITTED offset=(\d+)", logs)]

    @staticmethod
    def _resume_offsets(logs: str) -> list[int]:
        return [int(x) for x in re.findall(r"RESUMING FROM offset=(\d+)", logs)]

    def _await_progress_past(self, poison: int, timeout: int):
        """Poll consumer logs until a COMMITTED offset > poison appears.

        Returns (max_committed_offset, logs) on success, or (None, "") on
        timeout. Tolerates the consumer pod's start-up / pip-install lag.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._running_consumer_pods():
                logs = self._logs()
                committed = self._committed_offsets(logs)
                if committed and max(committed) > poison:
                    return max(committed), logs
            time.sleep(10)
        return None, ""

    def evaluate(self) -> dict:
        print("== Data-Plane Progress Oracle ==")
        poison = getattr(self.problem, "poison_offset", None)
        if poison is None:
            return {"success": False, "reason": "poison_offset unknown (fault not injected?)"}

        if not self._consumer_pods():
            print("❌ No orders-validator consumer pod found")
            return {"success": False, "reason": "consumer deployment not present"}

        print(f"⏳ Settling {self.settle_seconds}s before evaluation...")
        time.sleep(self.settle_seconds)
        print(f"   Checking the consumer advanced past poison offset {poison}...")
        max_committed, logs = self._await_progress_past(poison, self.progress_timeout)
        if max_committed is None:
            print(f"❌ Consumer never committed past the poison offset {poison}")
            return {"success": False, "reason": "offset not advanced past poison record"}
        print(f"   max committed offset = {max_committed} (poison was {poison})")
        resumes = self._resume_offsets(logs)
        if resumes and resumes[-1] > poison + 1:
            print(
                f"❌ Consumer resumed at offset {resumes[-1]}, past poison+1 "
                f"({poison + 1}) — valid records were skipped"
            )
            return {"success": False, "reason": "data loss: valid records skipped"}

        time.sleep(self.progress_window_seconds)
        after = self._committed_offsets(self._logs())
        max_after = max(after) if after else -1
        print(f"   forward progress: {max_committed} -> {max_after}")
        if max_after <= max_committed:
            print("❌ Consumer offset is not advancing — no live forward progress")
            return {"success": False, "reason": "no forward progress"}

        running = self._running_consumer_pods()
        if not running:
            return {"success": False, "reason": "consumer not Running before restart probe"}
        victim = running[0].metadata.name
        print(f"🔁 Restart-resistance probe: deleting consumer pod {victim}")
        self.problem.kubectl.exec_command(
            f"kubectl delete pod {victim} -n {self.problem.namespace} --wait=true"
        )

        probe_max, _ = self._await_progress_past(poison, self.restart_recover_timeout)
        if probe_max is None:
            print("❌ Pipeline re-stalled after restart — the fix was not durable")
            return {"success": False, "reason": "not restart-resistant"}
        print(f"   post-restart max committed offset = {probe_max}")

        print("✅ Advanced past the poison record, no data loss, restart-resistant")
        return {"success": True}