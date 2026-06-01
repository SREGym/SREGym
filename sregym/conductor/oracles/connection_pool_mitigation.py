"""
Mitigation oracle for the database connection-pool-exhaustion problem.

The default MitigationOracle only checks that the namespace's pods are Running, which is useless here since 
the database pod and the dependent service pod both stay Running and Ready throughout, because neither 
carries a health probe. The fault lives in the database's incoming-connection cap, not in any pod's phase.

This oracle verifies the cap functionally. It opens a burst of connections to the database and counts how 
many succeed before the server refuses further ones. A pathologically low cap refuses after a handful; a 
healthy server accepts the whole burst. The count is read at connection-establishment time, which the server 
enforces before authentication, so the probe needs no database credentials. This makes the fix verifiable 
regardless of how the agent applies it (raise the cap, remove it, or move it to a config file) and rejects 
the shortcuts (restarting without changing the cap, scaling the database to zero, or deleting it).
"""

from __future__ import annotations

import json
import re
import time
from json import JSONDecodeError

from sregym.conductor.oracles.base import Oracle

_DEFAULT_TIMEOUT_SECONDS = 120
_DEFAULT_POLL_INTERVAL_SECONDS = 6
_DEFAULT_CONSECUTIVE_HEALTHY_POLLS = 2

# Opening a connection counts against the server's incoming-connection cap before any
# authentication, so this burst measures the cap without credentials.
_FLOOD_EVAL = (
    "var conns=[];var ok=0;"
    "for(var i=0;i<{attempts};i++){{"
    'try{{var c=new Mongo("127.0.0.1:27017");c.getDB("admin").runCommand({{ping:1}});conns.push(c);ok++;}}'
    "catch(e){{break;}}"
    "}}"
    'print("FLOOD_OK="+ok);'
)


def count_open_connections(kubectl, namespace: str, deployment: str, attempts: int) -> int:
    """Open up to ``attempts`` connections to the database and return how many succeeded."""
    eval_js = _FLOOD_EVAL.format(attempts=attempts)
    output = kubectl.exec_command(
        f"kubectl exec deploy/{deployment} -n {namespace} -- mongo --quiet --host 127.0.0.1 --eval '{eval_js}'"
    )
    match = re.search(r"FLOOD_OK=(\d+)", output)
    return int(match.group(1)) if match else 0


class ConnectionPoolMitigationOracle(Oracle):
    """Pass when the database accepts a healthy burst of connections again."""

    importance = 1.0

    def __init__(
        self,
        problem,
        *,
        db_deployment: str,
        consumer_deployment: str,
        probe_attempts: int,
        healthy_min_connections: int,
        timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS,
        poll_interval_seconds: int = _DEFAULT_POLL_INTERVAL_SECONDS,
        consecutive_successes: int = _DEFAULT_CONSECUTIVE_HEALTHY_POLLS,
    ):
        super().__init__(problem)
        self.db_deployment = db_deployment
        self.consumer_deployment = consumer_deployment
        self.probe_attempts = probe_attempts
        self.healthy_min_connections = healthy_min_connections
        self.timeout_seconds = timeout_seconds
        self.poll_interval_seconds = poll_interval_seconds
        self.consecutive_successes = consecutive_successes


    def evaluate(self) -> dict:
        print("== Connection Pool Mitigation Evaluation ==")

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

        for deployment in (self.db_deployment, self.consumer_deployment):
            spec, error = self._kubectl_json(
                f"kubectl get deployment {deployment} -n {namespace} --ignore-not-found -o json"
            )
            if error:
                return False, error
            if not spec:
                return False, (
                    f"Deployment/{deployment} no longer exists. Deleting or replacing it removes the workload "
                    "rather than restoring the connection capacity."
                )
            ready, detail = self._deployment_ready(spec, deployment)
            if not ready:
                return False, detail

        opened = count_open_connections(
            self.problem.kubectl, namespace, self.db_deployment, self.probe_attempts
        )
        if opened < self.healthy_min_connections:
            return False, (
                f"Deployment/{self.db_deployment} accepted only {opened} of {self.probe_attempts} probe "
                f"connections before refusing; the incoming-connection cap is still too low "
                f"(need at least {self.healthy_min_connections})."
            )

        return True, (
            f"Deployment/{self.db_deployment} accepted {opened}/{self.probe_attempts} probe connections "
            f"and Deployment/{self.consumer_deployment} is Ready"
        )


    def _deployment_ready(self, deployment: dict, name: str) -> tuple[bool, str]:
        desired = deployment.get("spec", {}).get("replicas", 1)
        status = deployment.get("status", {})
        ready = status.get("readyReplicas", 0)
        updated = status.get("updatedReplicas", 0)
        unavailable = status.get("unavailableReplicas", 0)

        if desired < 1:
            return False, f"Deployment/{name} is scaled to {desired} replicas; expected at least 1"

        if ready < desired or updated < desired or unavailable:
            return False, (
                f"Deployment/{name} is not Ready: "
                f"ready={ready}, updated={updated}, unavailable={unavailable}, desired={desired}"
            )

        return True, f"Deployment/{name} has {ready}/{desired} ready replicas"


    def _kubectl_json(self, command: str) -> tuple[dict | None, str | None]:
        output = self.problem.kubectl.exec_command(command)
        stripped = output.strip()

        # --ignore-not-found yields empty output for an absent object; report it as missing.
        if not stripped:
            return None, None

        try:
            return json.loads(stripped), None
        except JSONDecodeError as exc:
            return None, f"Failed to parse JSON from `{command}`: {exc}; output={stripped[:500]!r}"
        
