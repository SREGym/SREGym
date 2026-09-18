from types import SimpleNamespace

import pytest

from sregym.conductor.oracles.valkey_auth_mitigation import ValkeyAuthMitigation
from sregym.service.kubectl import KubeCtl


class _KubeCtl:
    def __init__(self, config_output: str, ping_output: str = "PONG\n", cart_available: int = 1):
        self.config_output = config_output
        self.ping_output = ping_output
        self.cart_available = cart_available

    def list_pods(self, namespace):
        pod = SimpleNamespace(metadata=SimpleNamespace(name="valkey-cart-abc123"))
        return SimpleNamespace(items=[pod])

    def exec_command(self, command):
        if command.endswith("CONFIG GET requirepass"):
            return self.config_output
        if command.endswith("valkey-cli PING"):
            return self.ping_output
        raise AssertionError(f"Unexpected command: {command}")

    def exec_command_checked(self, command, timeout=None):
        return self.exec_command(command)

    def get_deployment(self, name, namespace):
        assert name == "cart"
        return SimpleNamespace(
            metadata=SimpleNamespace(generation=1),
            spec=SimpleNamespace(replicas=1),
            status=SimpleNamespace(
                replicas=1,
                observed_generation=1,
                updated_replicas=1,
                ready_replicas=1,
                available_replicas=self.cart_available,
            ),
        )


def _evaluate(config_output: str, ping_output: str = "PONG\n", cart_available: int = 1) -> bool:
    problem = SimpleNamespace(
        namespace="astronomy-shop",
        kubectl=_KubeCtl(config_output, ping_output, cart_available),
    )
    return ValkeyAuthMitigation(problem).evaluate()["success"]


def test_accepts_cleared_password_with_blank_value_line():
    assert _evaluate("requirepass\n\n") is True


def test_accepts_cleared_password_when_cli_omits_blank_value_line():
    assert _evaluate("requirepass\n") is True


def test_rejects_nonempty_password():
    assert _evaluate("requirepass\ninvalid_pass\n") is False


def test_rejects_authentication_error_without_indexing_output():
    assert _evaluate("NOAUTH Authentication required.\n", "NOAUTH Authentication required.\n") is False


def test_requires_unauthenticated_ping():
    assert _evaluate("requirepass\n\n", "NOAUTH Authentication required.\n") is False


def test_requires_the_cart_deployment_to_recover():
    assert _evaluate("requirepass\n\n", cart_available=0) is False


def test_cart_scaled_to_zero_is_an_agent_error(monkeypatch):
    kube = _KubeCtl("requirepass\n")
    monkeypatch.setattr(
        kube,
        "get_deployment",
        lambda *_: SimpleNamespace(spec=SimpleNamespace(replicas=0), status=SimpleNamespace(available_replicas=0)),
    )
    result = ValkeyAuthMitigation(SimpleNamespace(namespace="app", kubectl=kube)).evaluate()
    assert result["reason"] == "required_deployment_scaled_to_zero"
    assert result["failure_class"] == "agent_error"


@pytest.mark.parametrize(
    "output",
    ["NOAUTH Authentication required.\n", "(error) NOAUTH Authentication required.\n", "requirepass\ninvalid_pass\n"],
)
def test_authentication_failure_is_not_an_unreadable_config(output):
    oracle = ValkeyAuthMitigation(SimpleNamespace(namespace="app", kubectl=_KubeCtl(output)))
    result = oracle.evaluate()
    assert result["reason"] == "valkey_still_requires_auth"
    assert result["failure_class"] == "agent_error"


def test_unexpected_ping_does_not_prove_an_authentication_failure():
    oracle = ValkeyAuthMitigation(
        SimpleNamespace(namespace="app", kubectl=_KubeCtl("requirepass\n", "LOADING dataset\n"))
    )
    result = oracle.evaluate()
    assert result["reason"] == "valkey_ping_failed"
    assert result["failure_class"] == "ambiguous"


@pytest.mark.parametrize("fail_on", ["CONFIG GET requirepass", "PING"])
def test_real_checked_command_failure_is_not_an_authentication_failure(monkeypatch, fail_on):
    import subprocess
    from types import MethodType

    kube = _KubeCtl("requirepass\n")
    kube.exec_command_checked = MethodType(KubeCtl.exec_command_checked, kube)

    def run(command, **kwargs):
        if command.endswith(fail_on):
            raise subprocess.CalledProcessError(1, command, stderr=b"connection refused")
        return subprocess.CompletedProcess(command, 0, stdout=b"requirepass\n")

    monkeypatch.setattr(subprocess, "run", run)
    result = ValkeyAuthMitigation(SimpleNamespace(namespace="app", kubectl=kube)).evaluate()
    assert result["success"] is False
    assert result["reason"] == "oracle_command_failed"
    assert result["failure_class"] == "ambiguous"
