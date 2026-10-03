"""SREGym-Lite data-tier faults re-targeted at the Incident Arena apps."""

from __future__ import annotations

import shlex
import time

from sregym.conductor.oracles.base import Oracle
from sregym.conductor.oracles.failure import FailureClass
from sregym.conductor.oracles.namespace_memory_limit_mitigation import NamespaceMemoryLimitMitigationOracle
from sregym.conductor.problems.base import Problem
from sregym.conductor.problems.lite_ia.k8s import ported
from sregym.conductor.problems.namespace_memory_limit import NamespaceMemoryLimit
from sregym.service.rollout import deployment_rollout_complete
from sregym.utils.decorators import mark_fault_injected


class RedisAuthMitigationOracle(Oracle):
    """The cache accepts unauthenticated clients again and its clients are up.

    Generalizes ``ValkeyAuthMitigation`` from Astronomy Shop's ``valkey-cart``
    to any Redis-protocol cache reached through ``problem.cache_ref``.
    """

    importance = 1.0
    FAILURE_CLASSES = {
        "cache_still_requires_auth": FailureClass.AGENT_ERROR,
        "cache_config_unreadable": FailureClass.AMBIGUOUS,
        "cache_ping_failed": FailureClass.AMBIGUOUS,
        "client_not_rolled_out": FailureClass.AMBIGUOUS,
    }

    @staticmethod
    def _requirepass_is_clear(output: str) -> bool:
        lines = output.splitlines()
        return bool(lines) and lines[0].strip() == "requirepass" and all(not line.strip() for line in lines[1:])

    @staticmethod
    def _authentication_error(output: str) -> bool:
        return output.strip().removeprefix("(error) ").startswith(("NOAUTH ", "WRONGPASS "))

    def evaluate(self, *args, **kwargs) -> dict:
        print("== Cache Authentication Mitigation Evaluation ==")
        problem = self.problem
        try:
            output = problem.cache_cli("CONFIG", "GET", "requirepass")
            if not self._requirepass_is_clear(output):
                if self._authentication_error(output) or output.splitlines()[:1] == ["requirepass"]:
                    print(f"❌ {problem.cache_ref} still requires authentication")
                    return self.fail("cache_still_requires_auth", cache=problem.cache_ref)
                print(f"❌ Unexpected CONFIG GET output: {output!r}")
                return self.fail("cache_config_unreadable", output=output.strip()[:200])
            ping = problem.cache_cli("PING").strip()
            if ping != "PONG":
                reason = "cache_still_requires_auth" if self._authentication_error(ping) else "cache_ping_failed"
                print(f"❌ PING returned {ping!r}")
                return self.fail(reason, ping=ping[:200])
            for name in problem.client_deployments:
                deployment = problem.kubectl.get_deployment(name, problem.namespace)
                if (deployment.spec.replicas or 0) < 1:
                    return self.fail("required_deployment_scaled_to_zero", deployment=name)
                if not deployment_rollout_complete(deployment):
                    print(f"❌ Client deployment {name} has not recovered")
                    return self.fail("client_not_rolled_out", deployment=name)
        except Exception as exc:
            print(f"❌ Error checking cache authentication: {exc}")
            return self.fail_from_exception(exc)
        print("✅ The cache accepts its clients again")
        return {"success": True}


class RedisAuthDisruptionIA(Problem):
    """An invalid ``requirepass`` on Slack Spine's shared Redis (was Astronomy Shop's valkey-cart).

    The chart's Redis runs without a password and every role connects without
    one. Injection sets a password at runtime and drops client connections so
    they reconnect into the authentication failure, as the original restarted
    the cart service.
    """

    def __init__(
        self,
        app_name: str = "slack_spine",
        cache_ref: str = "deploy/redis",
        container: str = "redis",
        cli: str = "redis-cli",
        client_deployments: tuple[str, ...] = ("svc-message", "svc-channel", "presence", "dispatcher"),
    ):
        self.cache_ref = cache_ref
        self.container = container
        self.cli = cli
        self.client_deployments = client_deployments
        self.faulty_service = cache_ref.split("/", 1)[1]
        self.bad_password = "invalid_pass"
        ported(
            self,
            app_name,
            component=f"service/{self.faulty_service}",
            description=(
                f"Authentication on the shared Redis (`{self.faulty_service}`) is broken: a `requirepass` was set "
                "at runtime (CONFIG SET) while every client connects without a password, and the clients' existing "
                "connections were dropped. Their reconnections fail with NOAUTH, so the presence, dispatch, "
                "real-time and application paths that use Redis fail. Clearing the password restores service."
            ),
            oracle_factory=RedisAuthMitigationOracle,
        )

    def cache_cli(self, *args: str, password: str | None = None) -> str:
        auth = f"REDISCLI_AUTH={shlex.quote(password)} " if password else ""
        command = f"{auth}{self.cli} --no-auth-warning " + " ".join(shlex.quote(a) for a in args)
        return self.kubectl.exec_command_checked(
            f"kubectl exec -n {self.namespace} {self.cache_ref} -c {self.container} -- sh -c {shlex.quote(command)}",
            timeout=30,
        )

    @mark_fault_injected
    def inject_fault(self):
        result = self.cache_cli("CONFIG", "SET", "requirepass", self.bad_password).strip()
        if result != "OK":
            raise RuntimeError(f"CONFIG SET requirepass failed: {result!r}")
        killed = self.cache_cli("CLIENT", "KILL", "TYPE", "normal", password=self.bad_password).strip()
        print(f"[FAULT INJECTED] {self.cache_ref} requires a password; dropped {killed} client connections")

    @mark_fault_injected
    def recover_fault(self):
        self.cache_cli("CONFIG", "SET", "requirepass", "", password=self.bad_password)
        print(f"[RECOVERED] {self.cache_ref} accepts unauthenticated clients again")


class StatefulSetMemoryLimitMitigationOracle(NamespaceMemoryLimitMitigationOracle):
    """``NamespaceMemoryLimitMitigationOracle`` for a StatefulSet target."""

    def _statefulset_ready(self):
        deadline = time.monotonic() + self.rollout_timeout_seconds
        while True:
            sts = self.problem.kubectl.apps_v1_api.read_namespaced_stateful_set(
                self.problem.faulty_service, self.problem.namespace
            )
            desired = 1 if sts.spec.replicas is None else sts.spec.replicas
            status = sts.status
            if (
                desired >= 1
                and (status.ready_replicas or 0) == desired
                and (status.updated_replicas or 0) == desired
                and (status.observed_generation or 0) >= (sts.metadata.generation or 0)
            ):
                return sts
            if time.monotonic() >= deadline:
                return None
            time.sleep(self.poll_interval_seconds)

    def evaluate(self, *args, **kwargs) -> dict:
        print("== Namespace Memory Limit Mitigation Evaluation ==")
        name = self.problem.faulty_service
        try:
            active_memory_quotas = self._active_memory_quotas()
            if active_memory_quotas:
                print(f"[FAIL] Namespace-wide memory admission requirements remain: {active_memory_quotas}")
                return self.fail("fault_still_present", quotas=sorted(active_memory_quotas))
            sts = self._statefulset_ready()
            if sts is None:
                print(f"[FAIL] StatefulSet '{name}' is not fully Ready")
                return self.fail("required_statefulset_not_ready", statefulset=name)
            if not self._service_has_ready_target_endpoint(sts):
                return self.fail("no_ready_endpoints", service=name)
            if not self._run_fresh_admission_and_connection_probe():
                print(f"[FAIL] A fresh no-memory-resource pod could not connect to {name}")
                return self.fail("fresh_pod_admission_failed", namespace=self.problem.namespace)
        except Exception as exc:
            print(f"[FAIL] Error checking namespace memory mitigation: {exc}")
            return self.fail_from_exception(exc)
        print(f"[PASS] Namespace memory admission is safe and {name} is reachable")
        return {"success": True}


class NamespaceMemoryLimitIA(Problem):
    """A namespace memory ResourceQuota blocks Saleor's recreated PostgreSQL pod.

    As in the original, the quota requires every container to declare memory.
    The database's own container does; its metrics-exporter sidecar does not, so
    admission rejects the recreated pod with ``must specify memory``.
    """

    QUOTA_NAME = NamespaceMemoryLimit.QUOTA_NAME
    MEMORY_QUOTA_KEYS = NamespaceMemoryLimit.MEMORY_QUOTA_KEYS
    MEMORY_LIMIT = NamespaceMemoryLimit.MEMORY_LIMIT
    rollout_timeout_seconds = 600

    def __init__(self, app_name: str = "saleor", statefulset: str = "postgres"):
        self.faulty_service = statefulset
        ported(
            self,
            app_name,
            component=f"resourcequota/{self.QUOTA_NAME}",
            description=(
                f"Namespace-wide ResourceQuota `{self.QUOTA_NAME}` (`memory: {self.MEMORY_LIMIT}`) requires "
                "every container to declare memory, but not every workload does. When the "
                f"`{statefulset}` StatefulSet's pod was recreated, admission rejected it with `must specify "
                "memory` because its metrics-exporter sidecar has no memory request (the database container "
                "itself does), so the database is down and every request that needs it fails. Other "
                "noncompliant pods are vulnerable on their next restart; the quota's 1Gi is also below what the "
                "namespace already requests."
            ),
            oracle_factory=StatefulSetMemoryLimitMitigationOracle,
        )

    def _pod_name(self) -> str:
        return f"{self.faulty_service}-0"

    @mark_fault_injected
    def inject_fault(self):
        quotas = self.kubectl.get_resource_quotas(self.namespace)
        existing = [q.metadata.name for q in quotas if set(q.spec.hard or {}) & self.MEMORY_QUOTA_KEYS]
        if existing:
            raise RuntimeError(f"Cannot inject over existing memory ResourceQuota: {existing}")
        self.kubectl.apply_resource(
            {
                "apiVersion": "v1",
                "kind": "ResourceQuota",
                "metadata": {"name": self.QUOTA_NAME, "namespace": self.namespace},
                "spec": {"hard": {"memory": self.MEMORY_LIMIT}},
            }
        )
        self.kubectl.exec_command_checked(
            f"kubectl delete pod {self._pod_name()} -n {self.namespace} --wait=true", timeout=180
        )
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            sts = self.kubectl.apps_v1_api.read_namespaced_stateful_set(self.faulty_service, self.namespace)
            if (sts.status.ready_replicas or 0) < (sts.spec.replicas or 1):
                print(f"StatefulSet {self.faulty_service} cannot recreate its pod under the quota")
                return
            time.sleep(2)
        raise RuntimeError(f"StatefulSet '{self.faulty_service}' remained ready after quota injection")

    @mark_fault_injected
    def recover_fault(self):
        quotas = self.kubectl.get_resource_quotas(self.namespace)
        if any(quota.metadata.name == self.QUOTA_NAME for quota in quotas):
            self.kubectl.delete_resource_quota(name=self.QUOTA_NAME, namespace=self.namespace)
        self.kubectl.exec_command_checked(
            f"kubectl rollout status statefulset/{self.faulty_service} -n {self.namespace} "
            f"--timeout={self.rollout_timeout_seconds}s",
            timeout=self.rollout_timeout_seconds + 30,
        )
