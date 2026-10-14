"""Slack Spine: feature flags."""

from __future__ import annotations

import json
import time

from sregym.conductor.oracles.base import Oracle
from sregym.conductor.oracles.failure import FailureClass
from sregym.conductor.problems.apps.slack_spine.helpers import (
    fetch_parallel,
    role_pod_ips,
    wait_rollout,
)


class SequencerModeOracle(Oracle):
    """The latent allocator is off (persisted and live) and concurrent sends get distinct sequence numbers.

    The probe plays the original's HTTP probe: the dormant path only shows
    when requests arrive, so it fires a burst of concurrent sends into one
    fresh channel and requires every send to succeed with a unique ``seq``.
    """

    importance = 1.0
    PROBE_SENDS = 24
    FAILURE_CLASSES = {
        "sends_failing": FailureClass.AGENT_ERROR,
        "probe_unavailable": FailureClass.AMBIGUOUS,
    }

    def evaluate(self, *args, **kwargs) -> dict:
        print("== Sequencer mode mitigation check ==")
        p = self.problem
        try:
            if not wait_rollout(p, p.faulty_service, timeout_s=180):
                return self.fail("required_deployment_not_rolled_out", deployment=p.faulty_service)
            dep = p.kubectl.get_deployment(p.faulty_service, p.namespace)
            env = {e.name: e.value for c in dep.spec.template.spec.containers for e in (c.env or [])}
            persisted = env.get(p.flag_key)
            if persisted not in (None, "", "atomic"):
                print(f"❌ Deployment {p.faulty_service} still sets {p.flag_key}={persisted}")
                return self.fail("fault_still_present", persisted=persisted)
            pods = role_pod_ips(p, "message")
            live = fetch_parallel(p, [(n, f"http://{ip}:8000/admin/sequencer", "GET", None) for n, ip in pods.items()])
            for pod, (status, body, _) in live.items():
                mode = json.loads(body).get("mode") if status == 200 else None
                print(f"[live] {pod} sequencer mode={mode}")
                if mode != "atomic":
                    return self.fail("fault_still_present", pod=pod, live_mode=mode)
            channel = f"seq-probe-{time.time_ns()}"
            sends = [
                (
                    f"send-{i}",
                    "http://svc-message:8000/messages",
                    "POST",
                    {"channel_id": channel, "client_msg_id": f"{channel}-{i}", "text": f"probe {i}"},
                )
                for i in range(self.PROBE_SENDS)
            ]
            replies = fetch_parallel(p, sends)
        except Exception as exc:
            print(f"❌ Probe failed: {exc}")
            return self.fail("probe_unavailable", error=str(exc))
        statuses = [int(status) for status, _, _ in replies.values()]
        seqs = [json.loads(body).get("seq") for status, body, _ in replies.values() if 200 <= int(status) < 300]
        print(f"[probe] {len(seqs)}/{self.PROBE_SENDS} sends ok, {len(set(seqs))} distinct seqs")
        if len(seqs) < self.PROBE_SENDS:
            return self.fail("sends_failing", statuses=sorted(statuses))
        if len(set(seqs)) != len(seqs) or sorted(seqs) != list(range(1, self.PROBE_SENDS + 1)):
            return self.fail("fault_still_present", seqs=sorted(seqs))
        print("✅ Atomic sequencer: concurrent sends got dense, unique sequence numbers")
        return {"success": True}
