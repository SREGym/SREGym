"""Slack Spine: cache."""

from __future__ import annotations

from sregym.conductor.oracles.base import Oracle
from sregym.conductor.oracles.failure import FailureClass
from sregym.service.rollout import deployment_rollout_complete


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
