"""Metastable retry storm on Slack Spine's channel -> workspace policy check.

Port of ``search_rate_retry_collapse_hotel_reservation``. The original puts an
aggressive retry policy (750 ms per try, 3 attempts, small backoff) on Hotel
Reservation's search -> rate RPC in front of a rate backend with bounded
capacity. The system is healthy at base load; a short burst fills the
backend's queue, calls start timing out, the retries (whose expired attempts
still occupy the backend) keep demand above capacity, and the outage persists
after the burst ends.

Slack Spine has the same shape on its send path. Every ``POST /messages`` on
svc-message resolves channel authz on svc-channel (``/authz/resolve``), which
revalidates the org policy on svc-workspace (``GET /orgs/:id/settings``)
through the servicekit mesh client (``roles.channel.mesh``). The port deploys

* svc-channel with an aggressive-but-plausible mesh policy towards workspace
  (3 attempts, retry on timeout, 750 ms per try, 50 ms backoff), and
* svc-workspace as the bounded backend: settings read uncached from Postgres
  (``SETTINGS_UNCACHED=1``, 100 ms per read) through a 6-connection pool, so
  about 60 reads/s against roughly 30 sends/s of offered load.

The trigger is the chart's own transient ``org_policy_revalidate`` runtime
event on svc-workspace (the ``write_retry_storm`` profile's trigger): for a
few seconds every read is 250 ms slower. The pool queue grows past the
per-try timeout, svc-channel retries the timed-out calls, and because an
abandoned attempt still waits for and holds a pooled connection, about three
attempts per send keep demand (~90/s) above capacity after the event clears.
Sends keep failing with ``503 authz_unavailable``.

The mitigation oracle bounds the knobs an agent could game (retry counts and
timeouts, breaker thresholds, the workspace connection budget, the policy check
and the trigger's own cost), requires normal traffic to recover, then replays
the trigger and requires the system to recover on its own and stay healthy.
"""

from __future__ import annotations

import contextlib
import json
import shlex
import time

import yaml
from kubernetes.client.rest import ApiException

from sregym.conductor.oracles.base import Oracle
from sregym.conductor.oracles.failure import FailureClass
from sregym.conductor.problems.base import Problem
from sregym.conductor.problems.lite_ia.k8s import ported
from sregym.service.rollout import deployment_rollout_complete
from sregym.utils.decorators import mark_fault_injected

# The chart's uniform, default-safe mesh policy (values.yaml roles.<role>.mesh).
DEFAULT_MESH = {
    "retries": 1,
    "retryOnTimeout": False,
    "perTryTimeoutMs": 3000,
    "backoffMs": 0,
    "breakerEnabled": False,
    "breakerThreshold": 1000000,
}

# Fetch a list of [key, url, method, body] from inside the cluster in one exec;
# prints {key: [status, body]}.
_FETCH_MANY = """
import json, sys, urllib.error, urllib.request
out = {}
for key, url, method, body in json.load(sys.stdin):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            out[key] = [resp.status, resp.read().decode("utf-8", "replace")]
    except urllib.error.HTTPError as exc:
        out[key] = [exc.code, exc.read().decode("utf-8", "replace")]
    except Exception as exc:
        out[key] = [0, repr(exc)]
print(json.dumps(out))
"""


def _prom_sum(text: str, name: str, **labels: str) -> float:
    """Sum every sample of ``name`` whose labels include ``labels``."""
    total = 0.0
    for line in text.splitlines():
        if not line.startswith(name) or line.startswith("#"):
            continue
        head, _, value = line.rpartition(" ")
        metric, _, label_text = head.partition("{")
        if metric != name:
            continue
        if any(f'{key}="{val}"' not in label_text for key, val in labels.items()):
            continue
        with contextlib.suppress(ValueError):
            total += float(value)
    return total


class RetryStormCollapseIA(Problem):
    """Aggressive mesh retries on svc-channel latch a retry storm on svc-workspace."""

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
        ported(
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
                    or not p.min_pool_timeout_s <= pool_timeout <= p.max_pool_timeout_s
                ):
                    print(
                        f"[FAIL] {pod}: pool outside the safe envelope (pool_size <= {p.max_pool_size}, "
                        f"max_overflow <= {p.max_overflow}, {p.min_pool_timeout_s:g} <= pool_timeout_s <= "
                        f"{p.max_pool_timeout_s:g})"
                    )
                    return self.fail("backend_pool_outside_safe_envelope", pod=pod, db=db)
            if connections > p.max_backend_connections:
                print(f"[FAIL] svc-{p.BACKEND_ROLE} holds {connections} connections (max {p.max_backend_connections})")
                return self.fail("backend_pool_outside_safe_envelope", connections=connections)

            channel_env, _ = self._container_env(p.CALLER_ROLE)
            message_env, _ = self._container_env(p.UPSTREAM_ROLE)
            if channel_env.get("WORKSPACE_POLICY_CHECK") != "1" or message_env.get("AUTHZ_CHECK") != "1":
                print("[FAIL] The send path no longer performs the authz / org policy checks")
                return self.fail("policy_check_disabled")

            workspace_env, replicas = self._container_env(p.BACKEND_ROLE)
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


__all__ = ["RetryStormCollapseIA", "RetryStormMitigationOracle"]
