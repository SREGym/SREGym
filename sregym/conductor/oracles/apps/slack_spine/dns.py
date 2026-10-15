"""Slack Spine: dns."""

from __future__ import annotations

import time

from sregym.conductor.oracles.base import Oracle
from sregym.conductor.oracles.failure import FailureClass
from sregym.conductor.problems.apps.slack_spine.helpers import (
    fetch_parallel,
    selector_of,
    wait_rollout,
)


class StaleHostAliasesOracle(Oracle):
    """Routing converged: no stale resolver entry, the rollout finished, every serving pod sends successfully.

    The fix must not depend on the retired entrypoint (it stays withdrawn)
    and must keep the edge's serving capacity (at least ``EDGE_REPLICAS``).
    """

    importance = 1.0
    # Let kept-alive connections and caches settle after the rollout, then
    # probe in rounds; the first fully successful round passes.
    SETTLE_S = 15.0
    PROBE_ROUNDS = 4
    ROUND_INTERVAL_S = 10.0
    FAILURE_CLASSES = {
        "rollout_paused_or_incomplete": FailureClass.AGENT_ERROR,
        "serving_capacity_reduced": FailureClass.AGENT_ERROR,
        "sends_failing": FailureClass.AGENT_ERROR,
        "probe_unavailable": FailureClass.AMBIGUOUS,
    }

    def evaluate(self, *args, **kwargs) -> dict:
        print("== Stale hostAliases mitigation check ==")
        p = self.problem
        try:
            dep = p.kubectl.get_deployment(p.faulty_service, p.namespace)
            if dep.spec.paused:
                print(f"❌ Deployment {p.faulty_service} rollout is still paused")
                return self.fail("rollout_paused_or_incomplete", paused=True)
            if (dep.spec.replicas or 0) < p.EDGE_REPLICAS:
                print(f"❌ {p.faulty_service} scaled to {dep.spec.replicas} (< {p.EDGE_REPLICAS})")
                return self.fail("serving_capacity_reduced", replicas=dep.spec.replicas)
            if not wait_rollout(p, p.faulty_service, timeout_s=180):
                return self.fail("rollout_paused_or_incomplete", paused=False)
            pods = [
                pod
                for pod in p.kubectl.core_v1_api.list_namespaced_pod(
                    p.namespace, label_selector=selector_of(p, p.faulty_service)
                ).items
                if pod.metadata.deletion_timestamp is None
            ]
            for pod in pods:
                for alias in pod.spec.host_aliases or []:
                    if p.TARGET_BACKEND in (alias.hostnames or []):
                        print(f"❌ Pod {pod.metadata.name} still pins {p.TARGET_BACKEND} to {alias.ip}")
                        return self.fail("fault_still_present", pod=pod.metadata.name, alias_ip=alias.ip)
            ready = {pod.metadata.name: pod.status.pod_ip for pod in pods if pod.status.pod_ip}
            if len(ready) < p.EDGE_REPLICAS:
                return self.fail("serving_capacity_reduced", pods=sorted(ready))
            time.sleep(self.SETTLE_S)
            failed: dict = {}
            for round_no in range(1, self.PROBE_ROUNDS + 1):
                if round_no > 1:
                    time.sleep(self.ROUND_INTERVAL_S)
                stamp = time.time_ns()  # fresh client_msg_ids every round
                sends = [
                    (
                        f"{name}#{i}",
                        f"http://{ip}:8000/messages",
                        "POST",
                        {
                            "channel_id": f"alias-probe-{i}",
                            "client_msg_id": f"alias-probe-{stamp}-{name}-{i}",
                            "text": "probe",
                        },
                    )
                    for name, ip in sorted(ready.items())
                    for i in range(3)
                ]
                replies = fetch_parallel(p, sends)
                failed = {
                    key: (status, body[:120])
                    for key, (status, body, _) in replies.items()
                    if not 200 <= int(status) < 300
                }
                print(f"[probe] round {round_no}: per-pod sends {len(replies) - len(failed)}/{len(replies)} ok")
                if not failed:
                    break
        except Exception as exc:
            print(f"❌ Probe failed: {exc}")
            return self.fail("probe_unavailable", error=str(exc))
        if failed:
            return self.fail("sends_failing", failed=failed)
        print("✅ Every serving svc-message pod resolves svc-auth through the current Service")
        return {"success": True}
