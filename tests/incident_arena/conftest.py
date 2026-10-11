import pytest


class StubKubeCtl:
    """Just enough of KubeCtl for constructing apps and problems offline."""

    def __init__(self):
        self.commands = []

    def exec_command(self, command, input_data=None):
        self.commands.append(command)
        # `kubectl get namespace ... -o name` -> pretend it exists.
        return "namespace/exists"

    def exec_command_checked(self, command, input_data=None, timeout=None):
        self.commands.append(command)
        return ""


@pytest.fixture
def offline_cluster(monkeypatch):
    """Construct Incident Arena apps/problems without a kubeconfig."""
    import sregym.service.apps.incident_arena.base as app_base

    monkeypatch.setattr(app_base, "KubeCtl", StubKubeCtl)
    return StubKubeCtl
