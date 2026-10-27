import time

from sregym.conductor.oracles.mitigation import MitigationOracle

_HEALTH_POLL_SECONDS = 5


class TrainTicketMitigationOracle(MitigationOracle):
    """Wait for current application health, not historical alert resolution."""

    def _wait_for_rollouts(self, kubectl, namespace):
        # Database sidecars can become briefly unready during recovery. Use the
        # existing settle budget for pod health as well as Deployment rollouts.
        deadline = time.monotonic() + self.rollout_time
        while time.monotonic() < deadline:
            if self._evaluate_current_state()["success"]:
                return
            time.sleep(min(_HEALTH_POLL_SECONDS, max(0, deadline - time.monotonic())))
        print("⚠️ Timed out waiting for application health; evaluating current state")

    def pods_unready(self, pods, **detail):
        active_pods = [
            pod
            for pod in pods
            if not (
                pod.status.phase == "Succeeded"
                and any(owner.kind == "Job" for owner in (pod.metadata.owner_references or []))
            )
        ]
        return super().pods_unready(active_pods, **detail)
