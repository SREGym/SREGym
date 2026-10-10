"""Injection of valkey_auth_disruption must verify that the fault is in effect.

``CONFIG SET requirepass`` lives only in server memory. If valkey-cart restarts
during injection (SREGym-Lite observed an OOMKill at the chart's 20Mi limit) the
password is gone and the mitigation oracle passes without agent action.
"""

from types import SimpleNamespace

import pytest

import sregym.generators.fault.inject_app as inject_app_module
from sregym.generators.fault.inject_app import ApplicationFaultInjector


def _pod(restarts: int):
    return SimpleNamespace(
        metadata=SimpleNamespace(name="valkey-cart-abc123"),
        status=SimpleNamespace(container_statuses=[SimpleNamespace(restart_count=restarts)]),
    )


class _KubeCtl:
    def __init__(self, restart_counts: list[int], config_get: str):
        self._restart_counts = list(restart_counts)
        self._config_get = config_get
        self.commands: list[str] = []
        self.checked_commands: list[str] = []

    def list_pods(self, namespace):
        restarts = self._restart_counts.pop(0) if len(self._restart_counts) > 1 else self._restart_counts[0]
        return SimpleNamespace(items=[_pod(restarts)])

    def exec_command(self, command):
        self.commands.append(command)
        return "OK\n"

    def exec_command_checked(self, command, timeout=None):
        self.checked_commands.append(command)
        return self._config_get


def _injector(kubectl):
    injector = object.__new__(ApplicationFaultInjector)
    injector.namespace = "astronomy-shop"
    injector.kubectl = kubectl
    return injector


def test_injection_verifies_requirepass_with_the_new_password(monkeypatch):
    monkeypatch.setattr(inject_app_module.time, "sleep", lambda _seconds: None)
    kubectl = _KubeCtl(restart_counts=[0, 0], config_get="requirepass\ninvalid_pass\n")

    _injector(kubectl).inject_valkey_auth_disruption()

    assert kubectl.commands[0].endswith("valkey-cli CONFIG SET requirepass 'invalid_pass'")
    assert kubectl.commands[1] == "kubectl delete pod -l app.kubernetes.io/name=cart -n astronomy-shop"
    assert len(kubectl.checked_commands) == 1
    assert "VALKEYCLI_AUTH=invalid_pass" in kubectl.checked_commands[0]
    assert kubectl.checked_commands[0].endswith("valkey-cli CONFIG GET requirepass")


def test_injection_fails_when_valkey_restarted(monkeypatch):
    monkeypatch.setattr(inject_app_module.time, "sleep", lambda _seconds: None)
    kubectl = _KubeCtl(restart_counts=[0, 1], config_get="requirepass\n\n")

    with pytest.raises(RuntimeError, match="restarted during injection"):
        _injector(kubectl).inject_valkey_auth_disruption()


def test_injection_fails_when_requirepass_is_not_set(monkeypatch):
    monkeypatch.setattr(inject_app_module.time, "sleep", lambda _seconds: None)
    kubectl = _KubeCtl(restart_counts=[0, 0], config_get="requirepass\n\n")

    with pytest.raises(RuntimeError, match="requirepass is not in effect"):
        _injector(kubectl).inject_valkey_auth_disruption()
