"""Slack Spine: retry storm."""

from __future__ import annotations

import json
import shlex
import time

import yaml

from sregym.conductor.oracles.apps.slack_spine.retry_storm import ChannelRetryStormOracle, RetryStormMitigationOracle
from sregym.conductor.problems.apps.slack_spine.helpers import _FETCH_MANY, _SPIKE_SCRIPT, APP, DEFAULT_MESH, _prom_sum
from sregym.conductor.problems.base import Problem
from sregym.conductor.problems.environment import configure_port, make_app
from sregym.service.apps.slack_spine.configuration import patch_role_db_config
from sregym.utils.decorators import mark_fault_injected


class RetryStormProblem(Problem):
    """Shared setup and operations for Slack Spine retry-storm faults."""

    CALLER_ROLE = "channel"  # mesh client with the aggressive retry policy
    BACKEND_ROLE = "workspace"  # bounded backend (uncached reads, small pool)
    UPSTREAM_ROLE = "message"  # one channel /authz/resolve call per send
    TRIGGER_EVENT = "org_policy_revalidate"
    CONTAINER = "app"

    caller_mesh = {
        "retries": 3,
        "retryOnTimeout": True,
        "perTryTimeoutMs": 750,
        "backoffMs": 50,
        "breakerEnabled": False,
        "breakerThreshold": 1000000,
    }
    backend_db = {"pool_size": 4, "max_overflow": 2}
    backend_env = {"SETTINGS_UNCACHED": "1", "SETTINGS_BASE_HOLD_MS": "100"}
    # Every arrival is a real send (POST /messages, then index + search
    # readback): the base of the chart's own retry-storm profile, at a steady
    # 30 sends/s (about half the backend's capacity).
    load_profile = ("lite_slack_send", {"base": "write", "cycles": [[30.0, 30.0, 30.0, 30.0]], "soak_cycles": 2})

    trigger_seconds = 10.0
    sample_seconds = 20.0
    post_trigger_settle_seconds = 20.0
    post_trigger_measure_seconds = 25.0

    # Safe operating envelope checked by the oracle (anti-gaming).
    max_retries = 3
    max_per_try_timeout_ms = 5000
    max_backoff_ms = 5000
    min_breaker_threshold = 3
    max_pool_size = 20  # the peer-uniform chart default
    max_overflow = 10
    min_pool_timeout_s = 1.0
    max_pool_timeout_s = 30.0
    max_backend_connections = 60  # replicas x (pool_size + max_overflow)
    min_settings_hold_ms = 250  # the trigger's cost (servicekit default)
    min_base_hold_ms = 100  # cost of an uncached settings read

    def __init__(self, app_name: str = "slack_spine"):
        super().__init__(app=make_app(app_name))
        self.app.set_load_profile(*self.load_profile)
        self.app.configure(
            {
                "app": {
                    "roles": {
                        self.CALLER_ROLE: {"mesh": dict(self.caller_mesh)},
                        self.BACKEND_ROLE: {"db": dict(self.backend_db), "env": dict(self.backend_env)},
                    }
                }
            }
        )
        self._injection_attempted = False

    # ------------------------------------------------------------------ cluster access
    def role_pod_ips(self, role: str) -> dict[str, str]:
        pods = self.kubectl.core_v1_api.list_namespaced_pod(self.namespace, label_selector=self.app.role_selector(role))
        return {
            pod.metadata.name: pod.status.pod_ip
            for pod in pods.items
            if pod.metadata.deletion_timestamp is None and pod.status.phase == "Running" and pod.status.pod_ip
        }

    def fetch_many(self, requests: list[tuple[str, str, str, object]], timeout: float = 120) -> dict:
        out = self.app.toolbox_exec(
            "python3 -c " + shlex.quote(_FETCH_MANY),
            input_data=json.dumps([list(item) for item in requests]),
            timeout=timeout,
        )
        return json.loads(out.strip().splitlines()[-1])

    def role_requests(self, role: str, path: str, method: str = "GET", body=None) -> list[tuple]:
        return [
            (f"{role}/{pod}{path}", f"http://{ip}:8000{path}", method, body)
            for pod, ip in sorted(self.role_pod_ips(role).items())
        ]

    def admin_all(self, role: str, path: str, method: str = "GET", body=None) -> dict[str, dict]:
        """Call an admin endpoint on every pod of ``role``; {pod_key: json}."""
        replies = self.fetch_many(self.role_requests(role, path, method, body))
        decoded = {}
        for key, (status, payload) in replies.items():
            if not 200 <= int(status) < 300:
                raise RuntimeError(f"{method} {key} returned {status}: {payload[:300]}")
            decoded[key] = json.loads(payload) if payload.strip() else {}
        if not decoded:
            raise RuntimeError(f"no running svc-{role} pods")
        return decoded

    def set_trigger(self, active: bool) -> None:
        self.admin_all(self.BACKEND_ROLE, "/admin/event", "PUT", {"name": self.TRIGGER_EVENT, "active": active})

    def metrics(self) -> dict[str, float]:
        """Mesh attempt and pool counters, summed over the pods of each role."""
        requests = (
            self.role_requests(self.UPSTREAM_ROLE, "/metrics")
            + self.role_requests(self.CALLER_ROLE, "/metrics")
            + self.role_requests(self.BACKEND_ROLE, "/metrics")
        )
        replies = self.fetch_many(requests)
        snapshot = {
            "authz_calls": 0.0,
            "backend_attempts": 0.0,
            "backend_timeouts": 0.0,
            "backend_ok": 0.0,
            "pool_checked_out": 0.0,
            "pool_capacity": 0.0,
        }
        for key, (status, text) in replies.items():
            if int(status) != 200:
                raise RuntimeError(f"GET {key} returned {status}: {text[:200]}")
            role = key.split("/", 1)[0]
            if role == self.UPSTREAM_ROLE:
                snapshot["authz_calls"] += _prom_sum(text, "http_client_attempts_total", target=self.CALLER_ROLE)
            elif role == self.CALLER_ROLE:
                target = self.BACKEND_ROLE
                snapshot["backend_attempts"] += _prom_sum(text, "http_client_attempts_total", target=target)
                snapshot["backend_timeouts"] += _prom_sum(
                    text, "http_client_attempts_total", target=target, result="timeout"
                )
                snapshot["backend_ok"] += _prom_sum(text, "http_client_attempts_total", target=target, result="ok")
            else:
                snapshot["pool_checked_out"] += _prom_sum(text, "db_pool_checked_out")
                snapshot["pool_capacity"] += _prom_sum(text, "db_pool_capacity")
        return snapshot

    def sample(self, seconds: float) -> dict:
        """Measure one window: user-visible error rate, retry amplification, pool pressure."""
        before = self.metrics()
        time.sleep(seconds)
        after = self.metrics()
        latest = self.app.wrk.latest_sent_s()
        users = self.app.wrk.summary(max(0.0, float(latest) - seconds)) if latest is not None else {}

        def delta(name):
            return after[name] - before[name]

        authz = delta("authz_calls")
        attempts = delta("backend_attempts")
        return {
            "offered": int(users.get("offered") or 0),
            "error_rate": float(users.get("error_rate") or 0.0),
            "authz_calls": authz,
            "backend_attempts": attempts,
            "amplification": attempts / authz if authz > 0 else (float("inf") if attempts else 0.0),
            "timeout_share": delta("backend_timeouts") / attempts if attempts > 0 else 0.0,
            "pool_checked_out": after["pool_checked_out"],
            "pool_capacity": after["pool_capacity"],
        }

    @staticmethod
    def describe(sample: dict) -> str:
        return (
            f"loadgen offered={sample['offered']} error_rate={sample['error_rate']:.1%} "
            f"authz={sample['authz_calls']:.0f} workspace_attempts={sample['backend_attempts']:.0f} "
            f"amplification={sample['amplification']:.2f} timeouts={sample['timeout_share']:.0%} "
            f"pool_in_use={sample['pool_checked_out']:.0f}/{sample['pool_capacity']:.0f}"
        )

    # ------------------------------------------------------------------ policy
    def set_caller_mesh(self, mesh: dict) -> None:
        """Apply ``mesh`` to every svc-channel pod now and persist it in app-config."""
        cm = self.kubectl.core_v1_api.read_namespaced_config_map(self.app.APP_CONFIG_MAP, self.namespace)
        document = yaml.safe_load(cm.data["app.yaml"])
        document["roles"][self.CALLER_ROLE]["mesh"] = dict(mesh)
        self.kubectl.core_v1_api.patch_namespaced_config_map(
            self.app.APP_CONFIG_MAP,
            self.namespace,
            {"data": {"app.yaml": yaml.safe_dump(document, sort_keys=False)}},
        )
        self.admin_all(self.CALLER_ROLE, "/admin/config", "PUT", {"mesh": dict(mesh)})

    # ------------------------------------------------------------------ injection
    def _verify_healthy_baseline(self) -> None:
        sample = self.sample(self.sample_seconds)
        print(f"[Baseline] {self.describe(sample)}")
        failures = []
        if sample["offered"] == 0 or sample["authz_calls"] == 0:
            failures.append("no send traffic")
        if sample["error_rate"] > 0.05:
            failures.append("users already see errors")
        if sample["amplification"] > 1.2:
            failures.append("the retry policy already amplifies healthy traffic")
        if sample["timeout_share"] > 0.02:
            failures.append("workspace calls already time out")
        if failures:
            raise RuntimeError("healthy vulnerable baseline was not established: " + "; ".join(failures))

    def _trigger(self, seconds: float) -> None:
        print(f"[Trigger] {self.TRIGGER_EVENT} on svc-{self.BACKEND_ROLE} for {seconds:.0f}s")
        self.set_trigger(True)
        try:
            time.sleep(seconds)
        finally:
            self.set_trigger(False)
        print("[Trigger] Event cleared")

    def _storm_sustained(self) -> tuple[bool, list[str]]:
        time.sleep(self.post_trigger_settle_seconds)
        sample = self.sample(self.post_trigger_measure_seconds)
        print(f"[Post-trigger] {self.describe(sample)}")
        failures = []
        if sample["error_rate"] < 0.5:
            failures.append("the user-visible failure did not persist after the trigger")
        if sample["amplification"] < 2.0:
            failures.append("workspace calls were not retry-amplified")
        if sample["timeout_share"] < 0.8:
            failures.append("workspace calls were not timing out")
        if sample["pool_checked_out"] < sample["pool_capacity"]:
            failures.append("the workspace pool did not stay saturated")
        return not failures, failures

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")
        if self._injection_attempted:
            raise RuntimeError("fault injection is already active")
        self._injection_attempted = True
        try:
            self.set_trigger(False)
            # Deployed this way; re-arming keeps a re-injection after recover_fault faithful.
            self.set_caller_mesh(self.caller_mesh)
            self._verify_healthy_baseline()
            self._trigger(self.trigger_seconds)
            latched, failures = self._storm_sustained()
            if not latched:
                print(f"[Trigger] Storm did not latch ({'; '.join(failures)}); retrying with a longer trigger")
                self._trigger(2 * self.trigger_seconds)
                latched, failures = self._storm_sustained()
            if not latched:
                raise RuntimeError("metastable state was not established: " + "; ".join(failures))
        except Exception:
            try:
                self.set_trigger(False)
                self.set_caller_mesh(DEFAULT_MESH)
            except Exception as cleanup_error:
                print(f"[Cleanup] Failed to restore the safe mesh policy: {cleanup_error}")
            raise
        print(
            f"Fault: retry storm latched | caller svc-{self.CALLER_ROLE} -> backend svc-{self.BACKEND_ROLE} | "
            f"Namespace: {self.namespace}\n"
        )

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")
        self.set_trigger(False)
        self.set_caller_mesh(DEFAULT_MESH)
        self._injection_attempted = False
        print(f"Restored the default mesh policy on svc-{self.CALLER_ROLE}")


class RetryStormCollapseIA(RetryStormProblem):
    """Aggressive mesh retries on svc-channel latch a retry storm on svc-workspace."""

    def __init__(self, app_name: str = "slack_spine"):
        super().__init__(app_name)
        configure_port(
            self,
            app_name,
            component=f"svc-{self.CALLER_ROLE} -> svc-{self.BACKEND_ROLE} mesh retry policy",
            description=(
                f"A metastable retry storm on the send path. Every message send (svc-message POST /messages) "
                f"resolves channel authz on svc-channel (/authz/resolve), which revalidates the org policy on "
                f"svc-workspace (GET /orgs/:id/settings) through svc-channel's mesh client. That client's policy "
                f"(`roles.channel.mesh` in ConfigMap app-config, also returned by svc-channel GET /admin/config) "
                f"is aggressive: {self.caller_mesh['retries']} attempts, retryOnTimeout=true, "
                f"{self.caller_mesh['perTryTimeoutMs']} ms per-try timeout, {self.caller_mesh['backoffMs']} ms "
                f"backoff. svc-workspace is a bounded backend: settings are read uncached from Postgres "
                f"(SETTINGS_UNCACHED=1, {self.backend_env['SETTINGS_BASE_HOLD_MS']} ms per read) through a "
                f"{self.backend_db['pool_size'] + self.backend_db['max_overflow']}-connection pool (pool_size "
                f"{self.backend_db['pool_size']} + max_overflow {self.backend_db['max_overflow']}), about 60 "
                "reads/s against about 30 sends/s, so normal traffic is healthy. A transient "
                "`org_policy_revalidate` runtime event on svc-workspace (about 10 s, already cleared) made each "
                "read 250 ms slower; the pool's wait queue grew past the 750 ms per-try timeout, svc-channel "
                "retried the timed-out calls, and the abandoned attempts still waited for and held pooled "
                "connections. About three attempts per send keep demand (~90/s) above the pool's capacity after "
                "the event ended, so the queue stays full, every attempt times out, and sends fail with 503 "
                "`authz_unavailable` (svc-workspace pool saturated, svc-channel http_client_attempts_total "
                "dominated by timeouts). The sustaining cause is the timeout/retry/queue feedback loop, not the "
                "expired event, which reverting does not fix. The workspace pool is smaller than every peer role's "
                "(20 + 10), so a diagnosis naming that bounded capacity together with the retry amplification is "
                "correct; one that only calls the pool too small, without the retry/timeout feedback that keeps "
                "it saturated after the event, is incomplete. Valid mitigations bring the retry policy (stop "
                "retrying timeouts, fewer attempts, or a sane circuit breaker) and/or the workspace capacity "
                "(within the peer-uniform pool) into a safe envelope so the backlog drains and a repeated "
                "trigger is survived."
            ),
            oracle_factory=RetryStormMitigationOracle,
        )


class ChannelRetryStormBase(RetryStormProblem):
    """Metastable retry storm on svc-message's channel authz call (message -> channel).

    Ports of the BlueprintHotelReservation ``rpc`` retry storms (an RPC
    config with a very low timeout and many retries; a trigger tips the
    system; retries keep demand above the backend's capacity afterwards).
    The Lite port already uses channel -> workspace; these use the edge one
    hop up. Every send (``POST /messages``) resolves channel authz on
    svc-channel (``GET /authz/resolve``) through svc-message's mesh client
    (``roles.message.mesh``, deployed with ``caller_mesh``). svc-channel is the
    bounded backend: strict ACL reads (``ACTIVE_EVENTS=read_consistency_strict``,
    ``ACL_HOLD_MS=100``) hold a pooled Postgres connection per resolve, so
    the pool bounds its throughput; an abandoned attempt still waits for and
    holds a connection, so retries of timed-out attempts are pure extra load.
    """

    CALLER_ROLE = "message"
    BACKEND_ROLE = "channel"
    UPSTREAM_ROLE = "message"
    caller_mesh = {
        "retries": 3,
        "retryOnTimeout": True,
        "perTryTimeoutMs": 300,
        "backoffMs": 0,
        "breakerEnabled": False,
        "breakerThreshold": 1000000,
    }
    backend_env = {"ACTIVE_EVENTS": "read_consistency_strict", "ACL_HOLD_MS": "100"}
    backend_db = {"pool_size": 8, "max_overflow": 4}
    min_acl_hold_ms = 100
    # Every arrival is a real send (POST /messages, then index + search readback).
    load_profile = ("lite_slack_send", {"base": "write", "cycles": [[30.0, 30.0, 30.0, 30.0]], "soak_cycles": 2})

    # Both co-causes: the amplifying retry policy and the bounded channel pool.
    COMPONENT = (
        "svc-message -> svc-channel mesh retry policy (roles.message.mesh) and svc-channel DB pool "
        "(roles.channel.db, below the peers' 20+10)"
    )
    CO_CAUSES = (
        "The retry/timeout feedback loop and the bounded channel pool are co-causes; either may be called primary; "
        "a diagnosis is incomplete only if it omits the retry/timeout amplification, or blames the strict reads or "
        "the trigger (capacity cut / load spike) alone."
    )

    def __init__(self, description: str, component: str | None = None):
        super().__init__()
        configure_port(
            self,
            APP,
            component=component or self.COMPONENT,
            description=f"{description} {self.CO_CAUSES}",
            oracle_factory=ChannelRetryStormOracle,
        )

    # ------------------------------------------------------------------ metrics
    def metrics(self) -> dict[str, float]:
        requests = self.role_requests(self.CALLER_ROLE, "/metrics") + self.role_requests(self.BACKEND_ROLE, "/metrics")
        replies = self.fetch_many(requests)
        snap = {
            k: 0.0
            for k in (
                "authz_calls",
                "backend_attempts",
                "backend_timeouts",
                "backend_ok",
                "pool_checked_out",
                "pool_capacity",
            )
        }
        for key, (status, text) in replies.items():
            if int(status) != 200:
                raise RuntimeError(f"GET {key} returned {status}: {text[:200]}")
            if key.split("/", 1)[0] == self.CALLER_ROLE:
                target = self.BACKEND_ROLE
                snap["authz_calls"] += _prom_sum(
                    text, "http_request_duration_seconds_count", method="POST", route="/messages"
                )
                snap["backend_attempts"] += _prom_sum(text, "http_client_attempts_total", target=target)
                snap["backend_timeouts"] += _prom_sum(
                    text, "http_client_attempts_total", target=target, result="timeout"
                )
                snap["backend_ok"] += _prom_sum(text, "http_client_attempts_total", target=target, result="ok")
            else:
                snap["pool_checked_out"] += _prom_sum(text, "db_pool_checked_out")
                snap["pool_capacity"] += _prom_sum(text, "db_pool_capacity")
        return snap

    @staticmethod
    def describe(sample: dict) -> str:
        return (
            f"loadgen offered={sample['offered']} error_rate={sample['error_rate']:.1%} "
            f"sends={sample['authz_calls']:.0f} channel_attempts={sample['backend_attempts']:.0f} "
            f"amplification={sample['amplification']:.2f} timeouts={sample['timeout_share']:.0%} "
            f"pool_in_use={sample['pool_checked_out']:.0f}/{sample['pool_capacity']:.0f}"
        )

    # ------------------------------------------------------------------ knobs
    def set_backend_pool(self, pool_size: int, max_overflow: int) -> None:
        """Resize svc-channel's pool live and persist it in app-config."""
        cm = self.kubectl.core_v1_api.read_namespaced_config_map(self.app.APP_CONFIG_MAP, self.namespace)
        text = patch_role_db_config(
            cm.data["app.yaml"], self.BACKEND_ROLE, {"pool_size": pool_size, "max_overflow": max_overflow}
        )
        self.kubectl.core_v1_api.patch_namespaced_config_map(
            self.app.APP_CONFIG_MAP, self.namespace, {"data": {"app.yaml": text}}
        )
        self.admin_all(
            self.BACKEND_ROLE, "/admin/config", "PUT", {"db": {"pool_size": pool_size, "max_overflow": max_overflow}}
        )
        self.admin_all(self.BACKEND_ROLE, "/admin/reload", "POST")

    def set_trigger(self, active: bool) -> None:  # no runtime event of our own to clear
        return None

    def tip(self) -> None:
        raise NotImplementedError

    def _arm(self) -> None:
        return None

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")
        try:
            self.set_caller_mesh(self.caller_mesh)
            self._verify_healthy_baseline()
            self._arm()
            self.tip()
            latched, failures = self._storm_sustained()
            if not latched:
                print(f"[Trigger] Storm did not latch ({'; '.join(failures)}); repeating the trigger")
                self.tip()
                latched, failures = self._storm_sustained()
            if not latched:
                raise RuntimeError("metastable state was not established: " + "; ".join(failures))
        except Exception:
            try:
                self.set_caller_mesh(DEFAULT_MESH)
            except Exception as cleanup_error:
                print(f"[Cleanup] Failed to restore the safe mesh policy: {cleanup_error}")
            raise
        print(f"Fault: retry storm latched | svc-message -> svc-channel | Namespace: {self.namespace}\n")

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")
        self.set_caller_mesh(DEFAULT_MESH)
        self._injection_attempted = False
        print("Restored the default mesh policy on svc-message")


class CapacityDecreaseRPCRetryStormSlack(ChannelRetryStormBase):
    """Trigger: a permanent cut of svc-channel's pool plus a transient latency pulse.

    The original injects a transient latency/CPU-stress trigger and applies a
    permanent capacity restraint. Here svc-channel's pool is cut from 8+4 to
    ``CUT_POOL`` (persisted in app-config) and a 10 s ``org_policy_revalidate``
    event on svc-workspace adds ~250 ms to every resolve (the org-policy check
    precedes the ACL read), pushing it past the 300 ms per-try timeout.
    """

    CUT_POOL = (6, 1)
    TRIGGER_EVENT = "org_policy_revalidate"
    TRIGGER_ROLE = "workspace"

    def __init__(self):
        mesh = self.caller_mesh
        super().__init__(
            description=(
                "A metastable retry storm on the send path after a capacity decrease. Every message send "
                "(svc-message POST /messages) resolves channel authz on svc-channel (GET /authz/resolve) through "
                "svc-message's mesh client, whose policy (`roles.message.mesh` in ConfigMap app-config, also returned "
                f"by svc-message GET /admin/config) is aggressive: {mesh['retries']} attempts, retryOnTimeout=true, "
                f"{mesh['perTryTimeoutMs']} ms per-try timeout, no backoff. svc-channel reads ACLs in strict mode "
                f"(ACTIVE_EVENTS=read_consistency_strict, ACL_HOLD_MS={self.backend_env['ACL_HOLD_MS']}): each "
                "resolve holds a pooled Postgres connection, so its pool bounds throughput. Its capacity was "
                f"permanently decreased: the pool was cut from {self.backend_db['pool_size']}+"
                f"{self.backend_db['max_overflow']} to {self.CUT_POOL[0]}+{self.CUT_POOL[1]} connections "
                "(`roles.channel.db` in app-config, live on svc-channel), still enough for normal traffic. A transient "
                "slowdown (a ~10 s `org_policy_revalidate` event on svc-workspace, already cleared) pushed resolves "
                "past the 300 ms per-try timeout; svc-message retried the timed-out calls while the abandoned attempts "
                "still waited for and held channel connections, so ~3 attempts per send keep demand above the reduced "
                "pool's capacity after the event ended: the pool stays saturated, every attempt times out and sends "
                "fail with 503 `authz_unavailable`. The sustaining cause is the timeout/retry/queue feedback loop "
                "(made self-sustaining by the capacity cut), not the expired event. Valid mitigations bring the retry "
                "policy (fewer attempts, a per-try timeout above the queueing delay, no retry on timeout, or a sane "
                "breaker) and/or svc-channel's pool (at most 60 backend connections across replicas) into a safe "
                "envelope so the "
                "backlog drains and a repeated slowdown is survived; disabling strict ACL reads or the authz check is "
                "not a fix."
            ),
        )

    def _arm(self) -> None:
        print(f"[Capacity] svc-channel pool -> {self.CUT_POOL[0]}+{self.CUT_POOL[1]} (permanent)")
        self.set_backend_pool(*self.CUT_POOL)

    def _pulse(self, active: bool) -> None:
        self.admin_all(self.TRIGGER_ROLE, "/admin/event", "PUT", {"name": self.TRIGGER_EVENT, "active": active})

    def tip(self) -> None:
        print(f"[Trigger] {self.TRIGGER_EVENT} on svc-{self.TRIGGER_ROLE} for {self.trigger_seconds:.0f}s")
        self._pulse(True)
        try:
            time.sleep(self.trigger_seconds)
        finally:
            self._pulse(False)
        print("[Trigger] Event cleared")

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")
        self._pulse(False)
        self.set_caller_mesh(DEFAULT_MESH)
        self.set_backend_pool(self.backend_db["pool_size"], self.backend_db["max_overflow"])
        print("Restored the default mesh policy on svc-message and svc-channel's pool")


class LoadSpikeRPCRetryStormSlack(ChannelRetryStormBase):
    """Trigger: a one-shot load spike of extra senders (the original: 30 s at a multiple of the base rate)."""

    backend_db = {"pool_size": 6, "max_overflow": 2}
    SPIKE_RPS = 60
    SPIKE_SECONDS = 30

    def __init__(self):
        mesh = self.caller_mesh
        super().__init__(
            description=(
                "A metastable retry storm on the send path triggered by a load spike. Every message send "
                "(svc-message POST /messages) resolves channel authz on svc-channel (GET /authz/resolve) through "
                "svc-message's mesh client, whose policy (`roles.message.mesh` in ConfigMap app-config, also returned "
                f"by svc-message GET /admin/config) is aggressive: {mesh['retries']} attempts, retryOnTimeout=true, "
                f"{mesh['perTryTimeoutMs']} ms per-try timeout, no backoff. svc-channel reads ACLs in strict mode "
                f"(ACTIVE_EVENTS=read_consistency_strict, ACL_HOLD_MS={self.backend_env['ACL_HOLD_MS']}) through a "
                f"{self.backend_db['pool_size']}+{self.backend_db['max_overflow']}-connection pool (`roles.channel.db`), "
                "enough for base traffic. A transient burst of sends (an extra ~"
                f"{self.SPIKE_RPS}/s for {self.SPIKE_SECONDS} s, already over) queued resolves past the 300 ms per-try "
                "timeout; svc-message retried the timed-out calls while the abandoned attempts still waited for and "
                "held channel connections, so ~3 attempts per send keep demand above the pool's capacity at base "
                "load: the pool stays saturated, every attempt times out and sends fail with 503 "
                "`authz_unavailable`. The sustaining cause is the timeout/retry/queue feedback loop, not the spike. "
                "Valid mitigations bring the retry policy (fewer attempts, a per-try timeout above the queueing delay, "
                "no retry on timeout, or a sane breaker) and/or svc-channel's pool (at most 60 backend connections "
                "across replicas) into a safe envelope so the backlog drains and a repeated spike is survived; disabling strict ACL "
                "reads or the authz check is not a fix."
            ),
        )

    def tip(self) -> None:
        print(f"[Trigger] load spike: +{self.SPIKE_RPS} sends/s for {self.SPIKE_SECONDS}s")
        script = _SPIKE_SCRIPT.format(rps=self.SPIKE_RPS, seconds=self.SPIKE_SECONDS)
        print(self.app.toolbox_exec("python3 -c " + shlex.quote(script), timeout=self.SPIKE_SECONDS + 60).strip())
