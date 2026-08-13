from types import SimpleNamespace

from sregym.conductor.oracles.valkey_auth_mitigation import ValkeyAuthMitigation


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

    def get_deployment(self, name, namespace):
        assert name == "cart"
        return SimpleNamespace(
            spec=SimpleNamespace(replicas=1),
            status=SimpleNamespace(available_replicas=self.cart_available),
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
