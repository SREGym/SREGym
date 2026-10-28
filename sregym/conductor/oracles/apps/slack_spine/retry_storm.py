"""Slack Spine: retry storm."""

from __future__ import annotations

import json
import time

from kubernetes.client.rest import ApiException

from sregym.conductor.oracles.base import Oracle
from sregym.conductor.oracles.failure import FailureClass
from sregym.conductor.problems.apps.slack_spine.helpers import DEFAULT_MESH
from sregym.service.rollout import deployment_rollout_complete


class RetryStormMitigationOracle(Oracle):
    """Safe knobs, recovered traffic, and recovery from a replayed trigger."""

    importance = 1.0
    initial_recovery_timeout_s = 150.0
    replay_recovery_timeout_s = 120.0
    # After the replay the system must stay healthy this long (no slow re-latch).
    stable_after_replay_s = 60.0
    # Worst case: policy reads + 150 s recovery + 10 s replay + 120 s replay
    # recovery + 60 s stability, each sample overshooting by up to ~30 s, plus
    # the load generator health check that follows (~90 s). Well under 1200 s.
    evaluation_timeout_seconds = 720.0
    max_error_rate = 0.10
    max_amplification = 1.5
    max_timeout_share = 0.10

    FAILURE_CLASSES = {
        "retry_policy_outside_safe_envelope": FailureClass.AGENT_ERROR,
        "backend_pool_outside_safe_envelope": FailureClass.AGENT_ERROR,
        "policy_check_disabled": FailureClass.AGENT_ERROR,
        "trigger_neutralized": FailureClass.AGENT_ERROR,
        "traffic_did_not_recover": FailureClass.AGENT_ERROR,
        "did_not_recover_after_trigger": FailureClass.AGENT_ERROR,
        "metrics_unreadable": FailureClass.ENVIRONMENT_ERROR,
        "baseline_not_captured": FailureClass.HARNESS_ERROR,
    }

    def __init__(self, problem):
        super().__init__(problem)
        self._baseline_deployments: set[str] = set()

    def capture_baseline(self) -> None:
        deployments = self.problem.kubectl.apps_v1_api.list_namespaced_deployment(namespace=self.problem.namespace)
        self._baseline_deployments = {d.metadata.name for d in deployments.items}

    # ------------------------------------------------------------------ checks
    def _shape_unhealthy(self) -> dict | None:
        deployments = self.problem.kubectl.apps_v1_api.list_namespaced_deployment(namespace=self.problem.namespace)
        current = {d.metadata.name: d for d in deployments.items}
        missing = sorted(self._baseline_deployments - current.keys())
        if missing:
            print(f"[FAIL] Deployments are missing: {', '.join(missing)}")
            return self.fail("required_deployment_missing", deployments=missing)
        roles = (self.problem.UPSTREAM_ROLE, self.problem.CALLER_ROLE, self.problem.BACKEND_ROLE)
        unrolled = sorted(f"svc-{role}" for role in roles if not deployment_rollout_complete(current[f"svc-{role}"]))
        if unrolled:
            print(f"[FAIL] Deployments not rolled out and Ready: {', '.join(unrolled)}")
            return self.fail("required_deployment_not_rolled_out", deployments=unrolled)
        return None

    def _container_env(self, role: str) -> tuple[dict[str, str], int]:
        deployment = self.problem.kubectl.apps_v1_api.read_namespaced_deployment(f"svc-{role}", self.problem.namespace)
        container = next(c for c in deployment.spec.template.spec.containers if c.name == self.problem.CONTAINER)
        env = {item.name: item.value for item in container.env or [] if item.value is not None}
        return env, int(deployment.spec.replicas or 0)

    def _policy_outside_envelope(self) -> dict | None:
        p = self.problem
        try:
            role_env = {}
            for role in (p.UPSTREAM_ROLE, p.CALLER_ROLE, p.BACKEND_ROLE):
                try:
                    env, replicas = self._container_env(role)
                except ApiException as exc:
                    if exc.status == 404:
                        return self.fail("required_deployment_missing", deployment=f"svc-{role}")
                    raise
                if replicas < 1:
                    return self.fail("required_deployment_scaled_to_zero", deployment=f"svc-{role}")
                role_env[role] = env

            for role in (p.UPSTREAM_ROLE, p.CALLER_ROLE):
                for pod, config in p.admin_all(role, "/admin/config").items():
                    mesh = config.get("mesh") or {}
                    retries = int(mesh.get("retries", 1))
                    timeout_ms = float(mesh.get("perTryTimeoutMs", 0))
                    backoff_ms = float(mesh.get("backoffMs", 0))
                    breaker = bool(mesh.get("breakerEnabled", False))
                    threshold = int(mesh.get("breakerThreshold", DEFAULT_MESH["breakerThreshold"]))
                    print(f"[Policy] {pod} mesh={json.dumps(mesh, sort_keys=True)}")
                    if (
                        retries > p.max_retries
                        or timeout_ms > p.max_per_try_timeout_ms
                        or backoff_ms > p.max_backoff_ms
                        or (breaker and threshold < p.min_breaker_threshold)
                    ):
                        print(
                            f"[FAIL] {pod}: mesh policy outside the safe envelope (retries <= {p.max_retries}, "
                            f"perTryTimeoutMs <= {p.max_per_try_timeout_ms}, backoffMs <= {p.max_backoff_ms}, "
                            f"breakerThreshold >= {p.min_breaker_threshold} when enabled)"
                        )
                        return self.fail("retry_policy_outside_safe_envelope", pod=pod, mesh=mesh)

            backend = p.admin_all(p.BACKEND_ROLE, "/admin/config")
            connections = 0
            for pod, config in backend.items():
                db = config.get("db") or {}
                pool_size, overflow = int(db.get("pool_size", 0)), int(db.get("max_overflow", 0))
                pool_timeout = float(db.get("pool_timeout_s", 0))
                connections += pool_size + overflow
                print(f"[Policy] {pod} db={json.dumps(db, sort_keys=True)}")
                if (
                    not 0 < pool_size <= p.max_pool_size
                    or not 0 <= overflow <= p.max_overflow
                    or not 0 < pool_timeout <= p.max_pool_timeout_s
                ):
                    print(
                        f"[FAIL] {pod}: pool outside the safe envelope (pool_size <= {p.max_pool_size}, "
                        f"max_overflow <= {p.max_overflow}, 0 < pool_timeout_s <= "
                        f"{p.max_pool_timeout_s:g})"
                    )
                    return self.fail("backend_pool_outside_safe_envelope", pod=pod, db=db)
            if connections > p.max_backend_connections:
                print(f"[FAIL] svc-{p.BACKEND_ROLE} holds {connections} connections (max {p.max_backend_connections})")
                return self.fail("backend_pool_outside_safe_envelope", connections=connections)

            channel_env = role_env[p.CALLER_ROLE]
            message_env = role_env[p.UPSTREAM_ROLE]
            if channel_env.get("WORKSPACE_POLICY_CHECK") != "1" or message_env.get("AUTHZ_CHECK") != "1":
                print("[FAIL] The send path no longer performs the authz / org policy checks")
                return self.fail("policy_check_disabled")

            workspace_env = role_env[p.BACKEND_ROLE]
            strict_hold = float(workspace_env.get("SETTINGS_HOLD_MS", p.min_settings_hold_ms))
            uncached = workspace_env.get("SETTINGS_UNCACHED") == "1"
            base_hold = float(workspace_env.get("SETTINGS_BASE_HOLD_MS", 0))
            if strict_hold < p.min_settings_hold_ms or (uncached and base_hold < p.min_base_hold_ms):
                print("[FAIL] The settings read cost was lowered instead of fixing the feedback loop")
                return self.fail("trigger_neutralized", strict_hold_ms=strict_hold, base_hold_ms=base_hold)
        except ApiException as exc:
            return self.fail_from_exception(exc)
        except Exception as exc:
            print(f"[FAIL] The effective policy could not be read: {exc}")
            return self.fail("metrics_unreadable", error=f"{type(exc).__name__}: {exc}")
        return None

    def _unhealthy_sample(self) -> dict | None:
        unhealthy = self._shape_unhealthy()
        if unhealthy is not None:
            return unhealthy
        try:
            sample = self.problem.sample(self.problem.sample_seconds)
        except Exception as exc:
            print(f"[FAIL] Metrics could not be read: {exc}")
            return self.fail("metrics_unreadable", error=f"{type(exc).__name__}: {exc}")
        print(f"[Health] {self.problem.describe(sample)}")
        healthy = (
            sample["offered"] > 0
            and sample["authz_calls"] > 0
            and sample["error_rate"] <= self.max_error_rate
            and sample["amplification"] <= self.max_amplification
            and sample["timeout_share"] <= self.max_timeout_share
        )
        if healthy:
            return None
        return self.fail("traffic_did_not_recover", **sample)

    def _wait_for_healthy(self, timeout_s: float) -> dict | None:
        deadline = time.monotonic() + timeout_s
        last = None
        while time.monotonic() < deadline:
            last = self._unhealthy_sample()
            if last is None:
                return None
        return last if last is not None else self.fail("traffic_did_not_recover")

    def _stays_healthy(self, duration_s: float) -> dict | None:
        deadline = time.monotonic() + duration_s
        while time.monotonic() < deadline:
            unhealthy = self._unhealthy_sample()
            if unhealthy is not None:
                return unhealthy
        return None

    @staticmethod
    def _after_replay(verdict: dict) -> dict:
        if verdict.get("reason") == "traffic_did_not_recover":
            verdict = {**verdict, "reason": "did_not_recover_after_trigger"}
        return verdict

    def evaluate(self, *args, **kwargs) -> dict:
        print("== Retry Storm Mitigation Evaluation ==")
        if not self._baseline_deployments:
            print("[FAIL] No healthy baseline was captured")
            return self.fail("baseline_not_captured")
        try:
            outside = self._policy_outside_envelope()
            if outside is not None:
                return outside

            unhealthy = self._wait_for_healthy(self.initial_recovery_timeout_s)
            if unhealthy is not None:
                print("[FAIL] Normal send traffic did not recover")
                return unhealthy

            print(f"[Replay] Replaying the {self.problem.trigger_seconds:.0f}s trigger")
            self.problem._trigger(self.problem.trigger_seconds)
            unhealthy = self._wait_for_healthy(self.replay_recovery_timeout_s)
            if unhealthy is None:
                unhealthy = self._stays_healthy(self.stable_after_replay_s)
            if unhealthy is not None:
                print("[FAIL] The system did not recover (and stay recovered) after the replayed trigger")
                return self._after_replay(unhealthy)

            outside = self._policy_outside_envelope()
            if outside is not None:
                return outside
        except Exception as exc:
            print(f"[FAIL] Error while verifying mitigation: {exc}")
            return self.fail_from_exception(exc)
        print("[PASS] Sends are healthy and recover after replaying the temporary trigger")
        return {"success": True}


class ChannelRetryStormOracle(RetryStormMitigationOracle):
    """Safe svc-message mesh policy and svc-channel capacity, recovered sends, and survival of a replayed trigger."""

    def _policy_outside_envelope(self) -> dict | None:
        p = self.problem
        try:
            for pod, config in p.admin_all(p.CALLER_ROLE, "/admin/config").items():
                mesh = config.get("mesh") or {}
                retries = int(mesh.get("retries", 1))
                timeout_ms = float(mesh.get("perTryTimeoutMs", 0))
                backoff_ms = float(mesh.get("backoffMs", 0))
                breaker = bool(mesh.get("breakerEnabled", False))
                threshold = int(mesh.get("breakerThreshold", DEFAULT_MESH["breakerThreshold"]))
                print(f"[Policy] {pod} mesh={json.dumps(mesh, sort_keys=True)}")
                if (
                    retries > p.max_retries
                    or timeout_ms > p.max_per_try_timeout_ms
                    or backoff_ms > p.max_backoff_ms
                    or (breaker and threshold < p.min_breaker_threshold)
                ):
                    print(
                        f"[FAIL] {pod}: mesh policy outside the safe envelope (retries <= {p.max_retries}, "
                        f"perTryTimeoutMs <= {p.max_per_try_timeout_ms}, backoffMs <= {p.max_backoff_ms}, "
                        f"breakerThreshold >= {p.min_breaker_threshold} when enabled)"
                    )
                    return self.fail("retry_policy_outside_safe_envelope", pod=pod, mesh=mesh)
            connections = 0
            for pod, config in p.admin_all(p.BACKEND_ROLE, "/admin/config").items():
                db = config.get("db") or {}
                pool_size, overflow = int(db.get("pool_size", 0)), int(db.get("max_overflow", 0))
                pool_timeout = float(db.get("pool_timeout_s", 0))
                connections += pool_size + overflow
                print(f"[Policy] {pod} db={json.dumps(db, sort_keys=True)}")
                # No per-pod pool_size / max_overflow cap here: a larger pool on fewer replicas is a valid
                # fix; only the aggregate backend-connection budget below bounds it.
                if pool_size <= 0 or overflow < 0 or not 0 < pool_timeout <= p.max_pool_timeout_s:
                    print(f"[FAIL] {pod}: pool outside the safe envelope")
                    return self.fail("backend_pool_outside_safe_envelope", pod=pod, db=db)
            if connections > p.max_backend_connections:
                print(f"[FAIL] svc-{p.BACKEND_ROLE} holds {connections} connections (max {p.max_backend_connections})")
                return self.fail("backend_pool_outside_safe_envelope", connections=connections)
            message_env, _ = self._container_env(p.UPSTREAM_ROLE)
            if message_env.get("AUTHZ_CHECK") != "1":
                print("[FAIL] The send path no longer performs the channel authz check")
                return self.fail("policy_check_disabled")
            channel_env, _ = self._container_env(p.BACKEND_ROLE)
            strict_live = all(
                "read_consistency_strict" in (state.get("active") or [])
                for state in p.admin_all(p.BACKEND_ROLE, "/admin/event").values()
            )
            hold = float(channel_env.get("ACL_HOLD_MS", 250))
            if not strict_live or hold < p.min_acl_hold_ms:
                print("[FAIL] Strict ACL reads were turned off or made cheaper instead of fixing the feedback loop")
                return self.fail("trigger_neutralized", strict=strict_live, acl_hold_ms=hold)
        except ApiException as exc:
            return self.fail_from_exception(exc)
        except Exception as exc:
            print(f"[FAIL] The effective policy could not be read: {exc}")
            return self.fail("metrics_unreadable", error=f"{type(exc).__name__}: {exc}")
        return None

    def evaluate(self, *args, **kwargs) -> dict:
        # The replay is the problem's own trigger.
        problem = self.problem
        problem._trigger = lambda _seconds: problem.tip()
        try:
            return super().evaluate(*args, **kwargs)
        finally:
            del problem._trigger
