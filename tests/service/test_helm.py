from sregym.service.helm import Helm
from sregym.service import helm as helm_module


class _CompletedProcess:
    returncode = 0

    def communicate(self):
        return b"ok", b""


def test_helm_upgrade_does_not_use_kubectl_server_side_flags(monkeypatch):
    commands = []

    def fake_popen(command, stdout=None, stderr=None):
        commands.append(command)
        return _CompletedProcess()

    monkeypatch.setattr(helm_module.subprocess, "Popen", fake_popen)

    Helm.upgrade(
        release_name="social-network",
        chart_path="/charts/socialnetwork",
        namespace="social-network",
        values_file="/charts/socialnetwork/values.yaml",
        set_values={"mongodb.auth.enabled": "false"},
    )

    command = commands[0]
    assert "--server-side=true" not in command
    assert "--force-conflicts" not in command
    assert "--set" in command
    assert "mongodb.auth.enabled=false" in command
