"""Mitigation oracle for HPA control-plane health.

The default MitigationOracle only checks pod health, which is insufficient
here because pods can be Running/Ready while the HPA control loop is broken.
This oracle evaluates the live HPA status instead.

Restoring CPU requests is the canonical fix, but replacing the metric with a
valid raw CPU averageValue target is also accepted, as long as the HPA remains
correctly wired and healthy.
"""

from __future__ import annotations

import json
import time
from json import JSONDecodeError

from sregym.conductor.oracles.base import Oracle

_DEFAULT_TIMEOUT_SECONDS = 60
_DEFAULT_POLL_INTERVAL_SECONDS = 5
_DEFAULT_CONSECUTIVE_HEALTHY_POLLS = 2


class HPAControlPlaneMitigationOracle(Oracle):
    """Pass when the frontend HPA can compute CPU metrics again."""

    importance = 1.0

    def __init__(
        self,
        problem,
        *,
        deployment_name: str,
        hpa_name: str,
        timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS,
        poll_interval_seconds: int = _DEFAULT_POLL_INTERVAL_SECONDS,
        consecutive_successes: int = _DEFAULT_CONSECUTIVE_HEALTHY_POLLS,
    ):
        super().__init__(problem)
        self.deployment_name = deployment_name
        self.hpa_name = hpa_name
        self.timeout_seconds = timeout_seconds
        self.poll_interval_seconds = poll_interval_seconds
        self.consecutive_successes = consecutive_successes

    def evaluate(self) -> dict:
        print("== HPA Control Plane Mitigation Evaluation ==")

        # Multiple consecutive healthy polls prevent a transient pass right after a rollout.
        consecutive_healthy = 0
        last_detail = "not evaluated"
        deadline = time.monotonic() + self.timeout_seconds

        while True:
            healthy, detail = self._evaluate_once()
            last_detail = detail

            if healthy:
                consecutive_healthy += 1
                print(f"✅ Healthy HPA poll {consecutive_healthy}/{self.consecutive_successes}: {detail}")
                if consecutive_healthy >= self.consecutive_successes:
                    return {"success": True, "details": detail}
            else:
                if consecutive_healthy:
                    print("⚠️ HPA health regressed; resetting consecutive poll count")
                consecutive_healthy = 0
                print(f"❌ HPA not healthy: {detail}")

            if time.monotonic() >= deadline:
                return {
                    "success": False,
                    "details": (
                        f"Timed out after {self.timeout_seconds}s waiting for "
                        f"{self.consecutive_successes} consecutive healthy HPA polls. "
                        f"Last check: {last_detail}"
                    ),
                }

            time.sleep(self.poll_interval_seconds)

    def _evaluate_once(self) -> tuple[bool, str]:
        deployment, error = self._kubectl_json(
            f"kubectl get deployment {self.deployment_name} -n {self.problem.namespace} -o json"
        )
        if error:
            return False, error

        ready, detail = self._deployment_ready(deployment)
        if not ready:
            return False, detail

        hpas, error = self._kubectl_json(f"kubectl get hpa -n {self.problem.namespace} -o json")
        if error:
            return False, error

        target_hpas = [hpa for hpa in hpas.get("items", []) if self._hpa_targets_deployment(hpa, self.deployment_name)]
        if not target_hpas:
            return False, (
                f"No HorizontalPodAutoscaler targets Deployment/{self.deployment_name}. "
                "Deleting the HPA without replacing it is not a valid mitigation."
            )

        for hpa in target_hpas:
            healthy, detail = self._hpa_healthy(hpa)
            if not healthy:
                return False, detail

        hpa_names = ", ".join(hpa["metadata"]["name"] for hpa in target_hpas)
        return True, f"Deployment/{self.deployment_name} is Ready and HPA(s) [{hpa_names}] are healthy"

    def _deployment_ready(self, deployment: dict) -> tuple[bool, str]:
        desired = deployment.get("spec", {}).get("replicas", 1)
        status = deployment.get("status", {})
        ready = status.get("readyReplicas", 0)
        updated = status.get("updatedReplicas", 0)
        unavailable = status.get("unavailableReplicas", 0)

        if desired < 1:
            return False, f"Deployment/{self.deployment_name} has desired replicas={desired}; expected at least 1"

        if ready < desired or updated < desired or unavailable:
            return False, (
                f"Deployment/{self.deployment_name} rollout not ready: "
                f"ready={ready}, updated={updated}, unavailable={unavailable}, desired={desired}"
            )

        selector = self._selector_from_deployment(deployment)
        pods, error = self._kubectl_json(f"kubectl get pods -n {self.problem.namespace} -l '{selector}' -o json")
        if error:
            return False, error

        items = pods.get("items", [])
        if not items:
            return False, f"No pods found for Deployment/{self.deployment_name} selector {selector!r}"

        for pod in items:
            pod_name = pod.get("metadata", {}).get("name", "<unknown>")
            if pod.get("metadata", {}).get("deletionTimestamp"):
                return False, f"Pod {pod_name} is terminating"

            phase = pod.get("status", {}).get("phase")
            if phase != "Running":
                return False, f"Pod {pod_name} is in phase {phase}"

            for status in pod.get("status", {}).get("containerStatuses", []):
                if not status.get("ready", False):
                    return False, f"Container {status.get('name')} in pod {pod_name} is not Ready"

        return True, f"Deployment/{self.deployment_name} has {ready}/{desired} ready replicas"

    def _hpa_healthy(self, hpa: dict) -> tuple[bool, str]:
        hpa_name = hpa.get("metadata", {}).get("name", "<unknown>")

        if not self._has_cpu_metric_spec(hpa):
            return False, f"HPA/{hpa_name} does not have a CPU resource metric configured"

        scaling_range_ok, scaling_range_detail = self._has_valid_scaling_range(hpa)
        if not scaling_range_ok:
            return False, f"HPA/{hpa_name} has invalid scaling range: {scaling_range_detail}"

        metric_readable, metric_detail = self._current_cpu_metric_readable(hpa)
        if not metric_readable:
            return False, f"HPA/{hpa_name} cannot currently compute its CPU metric: {metric_detail}"

        scaling_active, reason, message = self._scaling_active_condition(hpa)
        if not scaling_active:
            return False, f"HPA/{hpa_name} ScalingActive is not True: reason={reason}; message={message}"

        if reason == "FailedGetResourceMetric":
            return False, f"HPA/{hpa_name} still reports FailedGetResourceMetric: {message}"

        return True, f"HPA/{hpa_name} has readable CPU metrics and ScalingActive=True"

    @staticmethod
    def _hpa_targets_deployment(hpa: dict, deployment_name: str) -> bool:
        ref = hpa.get("spec", {}).get("scaleTargetRef", {})
        return (
            ref.get("kind") == "Deployment"
            and ref.get("name") == deployment_name
            and ref.get("apiVersion", "apps/v1") == "apps/v1"
        )

    @staticmethod
    def _has_cpu_metric_spec(hpa: dict) -> bool:
        for metric in hpa.get("spec", {}).get("metrics", []):
            resource = metric.get("resource", {})
            target = resource.get("target", {})
            if (
                metric.get("type") == "Resource"
                and resource.get("name") == "cpu"
                and target.get("type") in {"Utilization", "AverageValue"}
            ):
                return True
        return False

    @staticmethod
    def _has_valid_scaling_range(hpa: dict) -> tuple[bool, str]:
        spec = hpa.get("spec", {})
        min_replicas = spec.get("minReplicas", 1)
        max_replicas = spec.get("maxReplicas", 0)

        # maxReplicas=1 means the HPA can never scale up, making it effectively a no-op.
        if max_replicas < 2:
            return False, f"maxReplicas={max_replicas}; expected at least 2"

        if max_replicas <= min_replicas:
            return False, f"minReplicas={min_replicas}, maxReplicas={max_replicas}; expected max > min"

        return True, f"minReplicas={min_replicas}, maxReplicas={max_replicas}"

    @staticmethod
    def _current_cpu_metric_readable(hpa: dict) -> tuple[bool, str]:
        current_metrics = hpa.get("status", {}).get("currentMetrics", [])
        if not current_metrics:
            return False, "status.currentMetrics is empty"

        for metric in current_metrics:
            resource = metric.get("resource", {})
            if metric.get("type") != "Resource" or resource.get("name") != "cpu":
                continue

            current = resource.get("current", {})
            if "averageUtilization" in current:
                return True, f"averageUtilization={current['averageUtilization']}"

            if "averageValue" in current:
                return True, f"averageValue={current['averageValue']}"

            return False, "CPU metric has neither averageUtilization nor averageValue"

        return False, "no current CPU resource metric is reported"

    @staticmethod
    def _scaling_active_condition(hpa: dict) -> tuple[bool, str, str]:
        for condition in hpa.get("status", {}).get("conditions", []):
            if condition.get("type") == "ScalingActive":
                return (
                    condition.get("status") == "True",
                    condition.get("reason", ""),
                    condition.get("message", ""),
                )

        return False, "ScalingActiveMissing", "HPA status.conditions has no ScalingActive condition"

    @staticmethod
    def _selector_from_deployment(deployment: dict) -> str:
        labels = deployment.get("spec", {}).get("selector", {}).get("matchLabels", {})
        if not labels:
            raise RuntimeError(f"Deployment/{deployment['metadata']['name']} does not have matchLabels selector")

        return ",".join(f"{key}={value}" for key, value in sorted(labels.items()))

    def _kubectl_json(self, command: str) -> tuple[dict | None, str | None]:
        output = self.problem.kubectl.exec_command(command)
        stripped = output.strip()

        if not stripped:
            return None, f"Command returned no output: {command}"

        try:
            return json.loads(stripped), None
        except JSONDecodeError as exc:
            return None, f"Failed to parse JSON from `{command}`: {exc}; output={stripped[:500]!r}"
