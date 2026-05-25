"""Mitigation oracle for CPU-throttling brownout problems."""

import json
import subprocess
import time
import urllib.parse

from sregym.conductor.oracles.base import Oracle
_PROMETHEUS_URL = "http://localhost:9090"

_THROTTLE_THRESHOLD = 0.10
_SETTLE_BUFFER_SECONDS = 120
_SAMPLE_COUNT = 3
_SAMPLE_INTERVAL_SECONDS = 15


class CpuThrottlingRatioOracle(Oracle):
    """Passes when the faulty service is no longer CFS-throttled."""

    importance = 2.0

    def __init__(self, problem, faulty_service: str, threshold: float = _THROTTLE_THRESHOLD):
        super().__init__(problem)
        self.faulty_service = faulty_service
        self.threshold = threshold

    def _get_cpu_limit(self) -> str | None:
        """Return the deployment's container[0] cpu limit, "" if none, None on error."""
        ns = self.problem.namespace
        cmd = (
            f"kubectl get deployment {self.faulty_service} -n {ns} "
            '-o jsonpath="{.spec.template.spec.containers[0].resources.limits.cpu}"'
        )
        try:
            out = self.problem.kubectl.exec_command(cmd)
        except Exception as exc:
            print(f"⚠️  Failed to read cpu limit from deployment spec: {exc}")
            return None
        return (out or "").strip().strip("'\"")

    def _promql(self) -> str:
        ns = self.problem.namespace
        selector = f'namespace="{ns}",pod=~"^{self.faulty_service}-.*",container!="",container!="POD"'
        return (
            f"sum(rate(container_cpu_cfs_throttled_periods_total{{{selector}}}[2m]))"
            f" / "
            f"sum(rate(container_cpu_cfs_periods_total{{{selector}}}[2m]))"
        )

    def _query_ratio(self) -> float | None:
        url = f"{_PROMETHEUS_URL}/api/v1/query?query={urllib.parse.quote(self._promql())}"
        cmd = [
            "kubectl", "exec", "-n", "observe",
            "deploy/prometheus-server", "-c", "prometheus-server",
            "--", "wget", "-qO-", url,
        ]
        try:
            raw = subprocess.check_output(cmd, text=True, timeout=20)
            payload = json.loads(raw)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
            print(f"⚠️  Failed to query Prometheus: {exc}")
            return None

        result = payload.get("data", {}).get("result", [])
        if not result:
            return None
        try:
            value = float(result[0]["value"][1])
        except (KeyError, IndexError, ValueError):
            return None
        if value != value:  
            return 0.0
        return value

    def evaluate(self, solution=None, trace=None, duration=None) -> dict:
        print("== CPU Throttling Oracle Evaluation ==")
        limit = self._get_cpu_limit()
        if limit == "":
            print("✅ CPU limit removed from the deployment — CFS throttling is impossible.")
            return {"success": True, "accuracy": 100.0}
        if limit is None:
            print("⚠️  Could not read deployment spec; falling back to the throttling metric.")
        else:
            print(f"ℹ️  CPU limit still set to {limit}; checking the throttling ratio…")

        print(f"⏳ Waiting {_SETTLE_BUFFER_SECONDS}s for post-mitigation metrics to accumulate…")
        time.sleep(_SETTLE_BUFFER_SECONDS)

        samples = []
        attempts = 0
        while len(samples) < _SAMPLE_COUNT and attempts < _SAMPLE_COUNT * 2:
            attempts += 1
            ratio = self._query_ratio()
            if ratio is not None:
                print(f"   sample {len(samples) + 1}/{_SAMPLE_COUNT}: throttling ratio = {ratio:.3f}")
                samples.append(ratio)
            time.sleep(_SAMPLE_INTERVAL_SECONDS)

        if not samples:
            print(
                "❌ No CFS throttling data from Prometheus while a CPU limit is still set. "
                "Verify the kubelet/cAdvisor scrape job and the pod selector."
            )
            return {"success": False, "accuracy": 0.0}

        worst = max(samples)
        success = worst < self.threshold
        accuracy = max(0.0, min(100.0, 100.0 * (1.0 - worst)))

        if success:
            print(f"✅ {self.faulty_service} is no longer CFS-throttled (max ratio {worst:.3f} < {self.threshold})")
        else:
            print(f"❌ {self.faulty_service} is still CFS-throttled (max ratio {worst:.3f} ≥ {self.threshold})")

        return {"success": success, "accuracy": accuracy}