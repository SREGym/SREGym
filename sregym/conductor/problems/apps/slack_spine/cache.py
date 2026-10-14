"""Slack Spine: cache."""

from __future__ import annotations

import shlex

from sregym.conductor.oracles.apps.slack_spine.cache import RedisAuthMitigationOracle
from sregym.conductor.problems.base import Problem
from sregym.conductor.problems.environment import configure_port
from sregym.utils.decorators import mark_fault_injected


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
        configure_port(
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
