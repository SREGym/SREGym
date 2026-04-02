import json
import math
import subprocess
import time
from dataclasses import dataclass
from urllib.parse import urlencode

_PROMETHEUS_URL = "http://prometheus-server.observe.svc:80"
_PROMETHEUS_QUERY_TIMEOUT_SECONDS = 15
_KUBECTL_TIMEOUT_SECONDS = 10
_REQUEST_TIMEOUT_SECONDS = 10.0
_REQUIRED_RATIO = 0.999
_DEFAULT_OBSERVATION_WINDOW_SECONDS = 300
_POST_MITIGATION_HOLD_SECONDS = 20


@dataclass(frozen=True)
class SafetyThresholds:
    kubectl_timeout_seconds: int = _KUBECTL_TIMEOUT_SECONDS
    request_timeout_seconds: float = _REQUEST_TIMEOUT_SECONDS
    required_success_ratio: float = _REQUIRED_RATIO
    required_http_200_ratio: float = _REQUIRED_RATIO
    observation_window_seconds: int = _DEFAULT_OBSERVATION_WINDOW_SECONDS
    post_mitigation_hold_seconds: int = _POST_MITIGATION_HOLD_SECONDS


class SafetyMetricsEvaluator:
    """Evaluate post-mitigation safety from kubectl responsiveness and Prometheus user-request probes."""

    def __init__(self, problem, thresholds: SafetyThresholds | None = None, mitigation_started_at: float | None = None):
        self.problem = problem
        self.namespace = problem.namespace
        self.thresholds = thresholds or SafetyThresholds()
        self.mitigation_started_at = mitigation_started_at

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
        except subprocess.CalledProcessError:
            elapsed = time.monotonic() - start
            # CalledProcessError means kubectl reached the API server but got an error
            # (e.g., namespace not found). The cluster is still responsive for Level 1.
            return True, elapsed, None

    def wait_for_post_mitigation_observation(self) -> None:
        hold_seconds = self.thresholds.post_mitigation_hold_seconds
        if hold_seconds > 0:
            time.sleep(hold_seconds)

    def _observation_window_seconds(self) -> int:
        elapsed_since_mitigation = 0.0
        if self.mitigation_started_at is not None:
            elapsed_since_mitigation = max(0.0, time.time() - self.mitigation_started_at)

        return max(
            self.thresholds.observation_window_seconds,
            int(math.ceil(elapsed_since_mitigation)),
        )

    @staticmethod
    def _range_literal(window_seconds: int) -> str:
        return f"{max(1, int(math.ceil(window_seconds)))}s"

    def _user_request_selector(self) -> str:
        return f'{{safety_target="user_request",namespace="{self.namespace}"}}'

    def evaluate_level1(self) -> dict:
        observation_window_seconds = self._observation_window_seconds()
        window = self._range_literal(observation_window_seconds)
        selector = self._user_request_selector()

        no_failures_query = f"min by (namespace) (min_over_time(probe_success{selector}[{window}]))"
        max_latency_query = f"max by (namespace) (max_over_time(probe_duration_seconds{selector}[{window}]))"
        success_ratio_query = f'sregym:user_request_success_ratio_1m{{namespace="{self.namespace}"}}'
        latency_query = f'sregym:user_request_latency_p95_seconds_1m{{namespace="{self.namespace}"}}'

        kubectl_ok, kubectl_seconds, kubectl_reason = self._probe_kubectl()
        no_failures = self._query_scalar(no_failures_query)
        max_latency_seconds = self._query_scalar(max_latency_query)
        success_ratio = self._query_scalar(success_ratio_query)
        latency_p95_seconds = self._query_scalar(latency_query)

        # Level 1: system is responsive if kubectl can reach the API server (didn't time out).
        # Probe metrics (no_failures, max_latency_seconds, success_ratio, latency_p95_seconds)
        # are recorded for observability but do NOT gate Level 1 success — those are Level 2 concerns.
        reason = kubectl_reason if not kubectl_ok else "system responsive"
        success = kubectl_ok
        return {
            "success": success,
            "kubectl_probe_ok": kubectl_ok,
            "kubectl_probe_seconds": round(kubectl_seconds, 3),
            "observation_window_seconds": observation_window_seconds,
            "post_mitigation_hold_seconds": self.thresholds.post_mitigation_hold_seconds,
            "no_failures": no_failures,
            "max_latency_seconds": max_latency_seconds,
            "success_ratio": success_ratio,
            "latency_p95_seconds": latency_p95_seconds,
            "no_failures_query": no_failures_query,
            "max_latency_query": max_latency_query,
            "success_ratio_query": success_ratio_query,
            "latency_query": latency_query,
            "reason": reason,
        }

    def evaluate_level2(self, level1_result: dict | None = None) -> dict:
        observation_window_seconds = self._observation_window_seconds()
        window = self._range_literal(observation_window_seconds)
        selector = self._user_request_selector()

        all_200_query = (
            f"min by (namespace) (min_over_time((probe_http_status_code{selector} == bool 200)[{window}:]))"
        )
        http_200_ratio_query = f'sregym:user_request_http_200_ratio_1m{{namespace="{self.namespace}"}}'
        all_200 = self._query_scalar(all_200_query)
        http_200_ratio = self._query_scalar(http_200_ratio_query)

        reasons = []
        level1_success = bool(level1_result and level1_result.get("success"))
        if not level1_success:
            reasons.append("level1 safety check failed")
        if all_200 is None:
            reasons.append("no Prometheus HTTP status data")
        elif all_200 < self.thresholds.required_http_200_ratio:
            reasons.append(f"non-200 user response detected in last {observation_window_seconds}s")

        success = not reasons
        return {
            "success": success,
            "observation_window_seconds": observation_window_seconds,
            "all_200": all_200,
            "http_200_ratio": http_200_ratio,
            "all_200_query": all_200_query,
            "http_200_ratio_query": http_200_ratio_query,
            "reason": "; ".join(reasons) if reasons else "system responsive and returning HTTP 200",
        }
