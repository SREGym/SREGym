"""
Mitigation oracle for the oversized-config-artifact problem.

The default MitigationOracle only checks that the namespace's pods are Running. That
is not enough here, and it is wrong in two directions:

* It would accept deleting the consumer entirely, because a namespace with the worker
  gone has only healthy pods left. Deleting the worker removes the function, it does
  not restore it.
* It would accept emptying the artifact, because the worker then loads zero entries and
  stays Running while doing nothing useful.

This oracle instead checks the invariant that actually defines the fix: the consumer
Deployment must still exist and be Ready, and the artifact it loads must still carry
its entries. That accepts the legitimate fixes (bring the artifact back within the
consumer's capacity, or raise that capacity) and rejects the shortcuts.
"""

from __future__ import annotations

import json
import time
from json import JSONDecodeError

from sregym.conductor.oracles.base import Oracle

_DEFAULT_TIMEOUT_SECONDS = 180
_DEFAULT_POLL_INTERVAL_SECONDS = 5
_DEFAULT_CONSECUTIVE_HEALTHY_POLLS = 2


class OversizedConfigMitigationOracle(Oracle):
    """Pass when the consumer is Ready again and its artifact still carries entries."""

    importance = 1.0

    def __init__(
        self,
        problem,
        *,
        deployment_name: str,
        config_name: str,
        config_key: str,
        timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS,
        poll_interval_seconds: int = _DEFAULT_POLL_INTERVAL_SECONDS,
        consecutive_successes: int = _DEFAULT_CONSECUTIVE_HEALTHY_POLLS,
    ):
        super().__init__(problem)
        self.deployment_name = deployment_name
        self.config_name = config_name
        self.config_key = config_key
        self.timeout_seconds = timeout_seconds
        self.poll_interval_seconds = poll_interval_seconds
        self.consecutive_successes = consecutive_successes


    def evaluate(self) -> dict:
        print("== Oversized Config Mitigation Evaluation ==")

        # Require several consecutive healthy polls so a restart that has not yet
        # crash-looped again is not mistaken for a fix.
        consecutive_healthy = 0
        last_detail = "not evaluated"
        deadline = time.monotonic() + self.timeout_seconds

        while True:
            healthy, detail = self._evaluate_once()
            last_detail = detail

            if healthy:
                consecutive_healthy += 1
                print(f"Healthy poll {consecutive_healthy}/{self.consecutive_successes}: {detail}")
                if consecutive_healthy >= self.consecutive_successes:
                    return {"success": True, "details": detail}
            else:
                if consecutive_healthy:
                    print("Health regressed; resetting consecutive poll count")
                consecutive_healthy = 0
                print(f"Not healthy: {detail}")

            if time.monotonic() >= deadline:
                return {
                    "success": False,
                    "details": (
                        f"Timed out after {self.timeout_seconds}s waiting for "
                        f"{self.consecutive_successes} consecutive healthy polls. Last check: {last_detail}"
                    ),
                }

            time.sleep(self.poll_interval_seconds)


    def _evaluate_once(self) -> tuple[bool, str]:
        namespace = self.problem.namespace

        deployment, error = self._kubectl_json(
            f"kubectl get deployment {self.deployment_name} -n {namespace} --ignore-not-found -o json"
        )
        if error:
            return False, error
        if not deployment:
            return False, (
                f"Deployment/{self.deployment_name} no longer exists. Deleting the consumer removes the "
                "workload rather than restoring it."
            )

        ready, detail = self._deployment_ready(deployment)
        if not ready:
            return False, detail

        config, error = self._kubectl_json(
            f"kubectl get configmap {self.config_name} -n {namespace} --ignore-not-found -o json"
        )
        if error:
            return False, error
        if not config:
            return False, f"ConfigMap/{self.config_name} no longer exists; the artifact is gone"

        entry_count = self._non_empty_entry_count(config.get("data", {}).get(self.config_key, ""))
        if entry_count < 1:
            return False, (
                f"ConfigMap/{self.config_name} key {self.config_key!r} is empty. Emptying the artifact keeps the "
                "consumer Running but removes its function; that is not a valid fix."
            )

        return True, (
            f"Deployment/{self.deployment_name} is Ready and ConfigMap/{self.config_name} still carries "
            f"{entry_count} entries"
        )


    def _deployment_ready(self, deployment: dict) -> tuple[bool, str]:
        desired = deployment.get("spec", {}).get("replicas", 1)
        status = deployment.get("status", {})
        ready = status.get("readyReplicas", 0)
        updated = status.get("updatedReplicas", 0)
        unavailable = status.get("unavailableReplicas", 0)

        if desired < 1:
            return False, (
                f"Deployment/{self.deployment_name} is scaled to {desired} replicas. Scaling the consumer to zero "
                "stops the crash loop without restoring the workload."
            )

        if ready < desired or updated < desired or unavailable:
            return False, (
                f"Deployment/{self.deployment_name} is not Ready: "
                f"ready={ready}, updated={updated}, unavailable={unavailable}, desired={desired}"
            )

        selector = ",".join(
            f"{key}={value}"
            for key, value in sorted(deployment.get("spec", {}).get("selector", {}).get("matchLabels", {}).items())
        )
        pods, error = self._kubectl_json(f"kubectl get pods -n {self.problem.namespace} -l '{selector}' -o json")
        if error:
            return False, error

        for pod in pods.get("items", []):
            pod_name = pod.get("metadata", {}).get("name", "<unknown>")
            phase = pod.get("status", {}).get("phase")
            if phase != "Running":
                return False, f"Pod {pod_name} is in phase {phase}"
            for container_status in pod.get("status", {}).get("containerStatuses", []):
                if not container_status.get("ready", False):
                    reason = (container_status.get("state", {}).get("waiting") or {}).get("reason", "not ready")
                    return False, f"Container {container_status.get('name')} in pod {pod_name} is {reason}"

        return True, f"Deployment/{self.deployment_name} has {ready}/{desired} ready replicas"


    @staticmethod
    def _non_empty_entry_count(value: str) -> int:
        return sum(1 for line in value.splitlines() if line.strip())

    def _kubectl_json(self, command: str) -> tuple[dict | None, str | None]:
        output = self.problem.kubectl.exec_command(command)
        stripped = output.strip()

        # --ignore-not-found returns empty output when the object is absent; report that as a
        # missing object rather than a parse error.
        if not stripped:
            return None, None

        try:
            return json.loads(stripped), None
        except JSONDecodeError as exc:
            return None, f"Failed to parse JSON from `{command}`: {exc}; output={stripped[:500]!r}"
        
        