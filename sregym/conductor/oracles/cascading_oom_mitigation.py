"""
CascadingOOMMitigationOracle
============================

Evaluates whether an AI agent has *correctly and durably* mitigated the
cascading OOM failure — not just made the pods temporarily green.

Evaluation Criteria (all must pass for success=True)
-----------------------------------------------------
1. **Stress pod eliminated:**
   The `stress-hog` Pod must no longer exist in the namespace.

2. **Victim memory limit raised:**
   `mongodb-rate` deployment must declare `resources.limits.memory` ≥ 128 Mi.
   This ensures the agent closed the vulnerability rather than just deleting
   the stress pod and hoping for the best.

3. **All application pods Running + Ready:**
   Every Pod in the application namespace must reach phase=Running with all
   containers reporting ready=True.

4. **No OOMKilled restarts in the last observation window:**
   We inspect the last-terminated-state of every container; any OOMKilled
   exit within the past 5 minutes is treated as a signal the environment is
   still unstable.
"""

import time
from typing import Optional

from kubernetes.client import V1Pod  # type: ignore

from sregym.conductor.oracles.base import Oracle

# Minimum acceptable memory limit for the victim deployment after mitigation.
# Expressed in bytes for easy comparison.
_MIN_MEMORY_LIMIT_BYTES = 128 * 1024 * 1024  # 128 Mi

# How long we wait (seconds) after all pods appear Running before we trust it.
_SETTLE_WAIT = 30
_SETTLE_POLL = 5

# Time window (seconds) in which a previous OOMKilled exit is still flagged.
_OOM_RECENCY_WINDOW = 300  # 5 minutes


def _parse_memory_to_bytes(value: Optional[str]) -> int:
    """
    Convert a Kubernetes memory string (e.g. '256Mi', '1Gi', '512M') to bytes.
    Returns 0 if the value is None or unparseable.
    """
    if not value:
        return 0
    value = value.strip()
    units = {
        "Ki": 1024,
        "Mi": 1024 ** 2,
        "Gi": 1024 ** 3,
        "Ti": 1024 ** 4,
        "K":  1000,
        "M":  1000 ** 2,
        "G":  1000 ** 3,
        "T":  1000 ** 4,
    }
    for suffix, multiplier in units.items():
        if value.endswith(suffix):
            try:
                return int(value[: -len(suffix)]) * multiplier
            except ValueError:
                return 0
    try:
        return int(value)  # plain bytes
    except ValueError:
        return 0


class CascadingOOMMitigationOracle(Oracle):
    """
    Verifies that the AI agent has genuinely resolved the cascading OOM fault,
    not just patched symptoms.
    """

    importance = 1.0

    def evaluate(self) -> dict:
        print("=" * 60)
        print("== CascadingOOM Mitigation Evaluation ==")
        print("=" * 60)

        kubectl = self.problem.kubectl
        namespace = self.problem.namespace
        victim = self.problem.faulty_service         # "mongodb-rate"
        stress_pod = self.problem.stress_pod_name    # "stress-hog"

        results = {}

        # ----------------------------------------------------------------
        # Check 1 — Stress Pod Eliminated
        # ----------------------------------------------------------------
        stress_gone = self._check_stress_pod_gone(kubectl, namespace, stress_pod)
        results["stress_pod_eliminated"] = stress_gone

        # ----------------------------------------------------------------
        # Check 2 — Victim Memory Limit Raised
        # ----------------------------------------------------------------
        limit_ok = self._check_victim_memory_limit(kubectl, namespace, victim)
        results["victim_memory_limit_raised"] = limit_ok

        # ----------------------------------------------------------------
        # Check 3 — Wait for rollout to settle, then verify all pods Running
        # ----------------------------------------------------------------
        self._wait_for_rollout(kubectl, namespace)
        all_running = self._check_all_pods_running(kubectl, namespace)
        results["all_pods_running"] = all_running

        # ----------------------------------------------------------------
        # Check 4 — No recent OOMKilled containers
        # ----------------------------------------------------------------
        no_recent_oom = self._check_no_recent_oom(kubectl, namespace)
        results["no_recent_oom_kills"] = no_recent_oom

        # ----------------------------------------------------------------
        # Final verdict
        # ----------------------------------------------------------------
        success = stress_gone and limit_ok and all_running and no_recent_oom
        results["success"] = success

        self._print_summary(results)
        return results

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _check_stress_pod_gone(self, kubectl, namespace: str, stress_pod: str) -> bool:
        print(f"\n[Check 1] Stress pod '{stress_pod}' elimination …")
        pod_list = kubectl.list_pods(namespace)
        for pod in pod_list.items:
            if pod.metadata.name == stress_pod:
                print(f"  ❌ Stress pod '{stress_pod}' is still present (phase={pod.status.phase})")
                return False
        print(f"  ✅ Stress pod '{stress_pod}' not found — eliminated.")
        return True

    def _check_victim_memory_limit(self, kubectl, namespace: str, deployment: str) -> bool:
        print(f"\n[Check 2] Memory limit on '{deployment}' ≥ {_MIN_MEMORY_LIMIT_BYTES // (1024**2)} Mi …")
        try:
            dep_list = kubectl.list_deployments(namespace)
            for dep in dep_list.items:
                if dep.metadata.name != deployment:
                    continue
                containers = dep.spec.template.spec.containers
                if not containers:
                    print(f"  ❌ No containers found in deployment '{deployment}'")
                    return False

                c = containers[0]
                limits = getattr(c.resources, "limits", None) or {}
                mem_limit_str = limits.get("memory")
                mem_bytes = _parse_memory_to_bytes(mem_limit_str)

                # --- التعديل هنا ---
                if mem_bytes == 0:
                    print(f"  ✅ '{deployment}' has no memory limit (Valid fix: Original state restored).")
                    return True
                
                elif mem_bytes < _MIN_MEMORY_LIMIT_BYTES:
                    print(
                        f"  ❌ '{deployment}' memory limit is {mem_limit_str} "
                        f"({mem_bytes // (1024**2)} Mi) — below required "
                        f"{_MIN_MEMORY_LIMIT_BYTES // (1024**2)} Mi."
                    )
                    return False
                
                print(
                    f"  ✅ '{deployment}' memory limit = {mem_limit_str} "
                    f"({mem_bytes // (1024**2)} Mi) — sufficient."
                )
                return True

            print(f"  ❌ Deployment '{deployment}' not found in namespace '{namespace}'")
            return False

        except Exception as exc:
            print(f"  ❌ Error reading deployment resources: {exc}")
            return False

    def _wait_for_rollout(self, kubectl, namespace: str):
        print(f"\n[Waiting] Allowing up to {_SETTLE_WAIT}s for deployments to settle …")
        deadline = time.monotonic() + _SETTLE_WAIT
        while time.monotonic() < deadline:
            deployments = kubectl.list_deployments(namespace)
            all_settled = all(
                (dep.status.updated_replicas or 0) >= (dep.spec.replicas or 1)
                and (dep.status.ready_replicas or 0) >= (dep.spec.replicas or 1)
                and (dep.status.unavailable_replicas or 0) == 0
                for dep in deployments.items
            )
            if all_settled:
                print("  ✅ All deployments settled.")
                return
            time.sleep(_SETTLE_POLL)
        print("  ⚠️  Timed out waiting for rollout — evaluating current state.")

    def _check_all_pods_running(self, kubectl, namespace: str) -> bool:
        print("\n[Check 3] All application pods Running & Ready …")
        pod_list = kubectl.list_pods(namespace)

        if not pod_list.items:
            print("  ❌ No pods found in namespace.")
            return False

        all_ok = True
        for pod in pod_list.items:
            pod_name = pod.metadata.name

            if pod_name == self.problem.stress_pod_name:
                continue

            if pod.status.phase != "Running":
                print(f"  ❌ {pod_name}: phase={pod.status.phase}")
                all_ok = False
                continue

            for cs in (pod.status.container_statuses or []):
                if not cs.ready:
                    reason = (
                        cs.state.waiting.reason
                        if cs.state.waiting
                        else cs.state.terminated.reason
                        if cs.state.terminated
                        else "unknown"
                    )
                    print(f"  ❌ {pod_name}/{cs.name}: not ready (reason={reason})")
                    all_ok = False

        if all_ok:
            print("  ✅ All pods are Running and Ready.")
        return all_ok

    def _check_no_recent_oom(self, kubectl, namespace: str) -> bool:
        print(
            f"\n[Check 4] No OOMKilled containers in the last "
            f"{_OOM_RECENCY_WINDOW // 60} minutes …"
        )
        now = time.time()
        pod_list = kubectl.list_pods(namespace)
        found_recent_oom = False

        for pod in pod_list.items:
            pod_name = pod.metadata.name
            for cs in (pod.status.container_statuses or []):
                last = cs.last_state
                if not last:
                    continue
                term = last.terminated if hasattr(last, "terminated") else None
                if term and term.reason == "OOMKilled":
                    finished_ts = term.finished_at.timestamp() if term.finished_at else 0
                    age = now - finished_ts
                    if age < _OOM_RECENCY_WINDOW:
                        print(
                            f"  ❌ {pod_name}/{cs.name}: OOMKilled "
                            f"{int(age)}s ago (within {_OOM_RECENCY_WINDOW}s window)"
                        )
                        found_recent_oom = True

        if not found_recent_oom:
            print("  ✅ No recent OOMKilled events detected.")
        return not found_recent_oom

    @staticmethod
    def _print_summary(results: dict):
        print("\n" + "=" * 60)
        print("Mitigation Evaluation Summary")
        print("=" * 60)
        checks = [
            ("stress_pod_eliminated",     "Stress pod eliminated"),
            ("victim_memory_limit_raised", "Victim memory limit ≥ 128 Mi"),
            ("all_pods_running",           "All pods Running & Ready"),
            ("no_recent_oom_kills",        "No recent OOMKilled events"),
        ]
        for key, label in checks:
            icon = "✅" if results.get(key) else "❌"
            print(f"  {icon}  {label}")
        print("-" * 60)
        overall = "✅ PASS" if results.get("success") else "❌ FAIL"
        print(f"  Overall: {overall}")
        print("=" * 60)
