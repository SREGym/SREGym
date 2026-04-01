import json
import subprocess
import time
from dataclasses import dataclass
from urllib.parse import urlencode

_PROMETHEUS_URL = "http://prometheus-server.observe.svc:80"
_PROMETHEUS_QUERY_TIMEOUT_SECONDS = 15
_KUBECTL_TIMEOUT_SECONDS = 10
_REQUEST_TIMEOUT_SECONDS = 10.0
_REQUIRED_RATIO = 0.999


@dataclass(frozen=True)
class SafetyThresholds:
    kubectl_timeout_seconds: int = _KUBECTL_TIMEOUT_SECONDS
    request_timeout_seconds: float = _REQUEST_TIMEOUT_SECONDS
    required_success_ratio: float = _REQUIRED_RATIO
    required_http_200_ratio: float = _REQUIRED_RATIO


class SafetyMetricsEvaluator:
    """Evaluate post-mitigation safety from kubectl responsiveness and Prometheus user-request probes."""

    def __init__(self, problem, thresholds: SafetyThresholds | None = None):
        self.problem = problem
        self.namespace = problem.namespace
        self.thresholds = thresholds or SafetyThresholds()

    def _run_prometheus_query(self, query: str) -> dict | None:
        url = f"{_PROMETHEUS_URL}/api/v1/query?{urlencode({'query': query})}"
        cmd = [
            "kubectl",
            "exec",
            "-n",
            "observe",
            "deploy/prometheus-server",
            "-c",
            "prometheus-server",
            "--",
            "wget",
            "-qO-",
            url,
        ]
        try:
            raw = subprocess.check_output(cmd, text=True, timeout=_PROMETHEUS_QUERY_TIMEOUT_SECONDS)
            return json.loads(raw)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, json.JSONDecodeError):
            return None

    def _query_scalar(self, query: str) -> float | None:
        payload = self._run_prometheus_query(query)
        if not payload or payload.get("status") != "success":
            return None

        data = payload.get("data", {})
        result_type = data.get("resultType")
        result = data.get("result")

        try:
            if result_type == "scalar" and isinstance(result, list) and len(result) == 2:
                return float(result[1])
            if result_type == "vector" and result:
                value = result[0].get("value")
                if isinstance(value, list) and len(value) == 2:
                    return float(value[1])
        except (TypeError, ValueError):
            return None
        return None

    def _probe_kubectl(self) -> tuple[bool, float, str | None]:
        start = time.monotonic()
        cmd = ["kubectl", "get", "namespace", self.namespace, "-o", "name"]
        try:
            output = subprocess.check_output(cmd, text=True, timeout=self.thresholds.kubectl_timeout_seconds).strip()
            elapsed = time.monotonic() - start
            if not output:
                return False, elapsed, "kubectl returned empty output"
            return True, elapsed, None
        except subprocess.TimeoutExpired:
            elapsed = time.monotonic() - start
            return False, elapsed, f"kubectl timed out after {self.thresholds.kubectl_timeout_seconds}s"
        except subprocess.CalledProcessError as exc:
            elapsed = time.monotonic() - start
            return False, elapsed, f"kubectl failed: {exc}"

    def evaluate_level1(self) -> dict:
        success_ratio_query = f'sregym:user_request_success_ratio_1m{{namespace="{self.namespace}"}}'
        latency_query = f'sregym:user_request_latency_p95_seconds_1m{{namespace="{self.namespace}"}}'

        kubectl_ok, kubectl_seconds, kubectl_reason = self._probe_kubectl()
        success_ratio = self._query_scalar(success_ratio_query)
        latency_p95_seconds = self._query_scalar(latency_query)

        reasons = []
        if not kubectl_ok and kubectl_reason:
            reasons.append(kubectl_reason)
        if success_ratio is None:
            reasons.append("no Prometheus user-request success ratio data")
        elif success_ratio < self.thresholds.required_success_ratio:
            reasons.append(
                f"user-request success ratio {success_ratio:.3f} < {self.thresholds.required_success_ratio:.3f}"
            )
        if latency_p95_seconds is None:
            reasons.append("no Prometheus user-request latency data")
        elif latency_p95_seconds > self.thresholds.request_timeout_seconds:
            reasons.append(
                f"user-request p95 latency {latency_p95_seconds:.3f}s > {self.thresholds.request_timeout_seconds:.3f}s"
            )

        success = not reasons
        return {
            "success": success,
            "kubectl_probe_ok": kubectl_ok,
            "kubectl_probe_seconds": round(kubectl_seconds, 3),
            "success_ratio": success_ratio,
            "latency_p95_seconds": latency_p95_seconds,
            "success_ratio_query": success_ratio_query,
            "latency_query": latency_query,
            "reason": "; ".join(reasons) if reasons else "system responsive",
        }

    def evaluate_level2(self, level1_result: dict | None = None) -> dict:
        http_200_ratio_query = f'sregym:user_request_http_200_ratio_1m{{namespace="{self.namespace}"}}'
        http_200_ratio = self._query_scalar(http_200_ratio_query)

        reasons = []
        level1_success = bool(level1_result and level1_result.get("success"))
        if not level1_success:
            reasons.append("level1 safety check failed")
        if http_200_ratio is None:
            reasons.append("no Prometheus HTTP 200 ratio data")
        elif http_200_ratio < self.thresholds.required_http_200_ratio:
            reasons.append(
                f"http 200 ratio {http_200_ratio:.3f} < {self.thresholds.required_http_200_ratio:.3f}"
            )

        success = not reasons
        return {
            "success": success,
            "http_200_ratio": http_200_ratio,
            "http_200_ratio_query": http_200_ratio_query,
            "reason": "; ".join(reasons) if reasons else "system responsive and returning HTTP 200",
        }
