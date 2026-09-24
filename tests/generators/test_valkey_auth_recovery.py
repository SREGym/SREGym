from types import SimpleNamespace

import sregym.generators.fault.inject_app as inject_app_module
from sregym.generators.fault.inject_app import ApplicationFaultInjector


class _RecordingKubeCtl:
    def __init__(self):
        self.commands = []

    def list_pods(self, namespace):
        pod = SimpleNamespace(metadata=SimpleNamespace(name="valkey-cart-abc123"))
        return SimpleNamespace(items=[pod])

    def exec_command(self, command):
        self.commands.append(command)
        return "OK\n"


def test_valkey_recovery_authenticates_with_the_injected_password(monkeypatch):
    monkeypatch.setattr(inject_app_module.time, "sleep", lambda _seconds: None)
    injector = object.__new__(ApplicationFaultInjector)
    injector.namespace = "astronomy-shop"
    injector.kubectl = _RecordingKubeCtl()

    injector.recover_valkey_auth_disruption()

    assert "VALKEYCLI_AUTH=invalid_pass" in injector.kubectl.commands[0]
    assert injector.kubectl.commands[0].endswith("valkey-cli CONFIG SET requirepass ''")
    assert injector.kubectl.commands[1] == ("kubectl delete pod -l app.kubernetes.io/name=cart -n astronomy-shop")
