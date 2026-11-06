import subprocess

import pytest

from sregym.service.kubectl import KubeCtl


@pytest.fixture
def kubectl():
    # These tests exercise subprocess handling, not SDK initialization.
    return object.__new__(KubeCtl)


def test_checked_command_returns_stdout(monkeypatch, kubectl):
    completed = subprocess.CompletedProcess(args="kubectl version", returncode=0, stdout=b"ok\n", stderr=b"")
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: completed)

    assert kubectl.exec_command_checked("kubectl version") == "ok\n"


def test_checked_command_raises_on_nonzero_exit(monkeypatch, kubectl):
    def fail(*args, **kwargs):
        raise subprocess.CalledProcessError(1, "kubectl apply", stderr=b"apply rejected")

    monkeypatch.setattr(subprocess, "run", fail)

    with pytest.raises(RuntimeError, match="apply rejected"):
        kubectl.exec_command_checked("kubectl apply")


def test_checked_command_raises_on_timeout(monkeypatch, kubectl):
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("kubectl exec", 30)

    monkeypatch.setattr(subprocess, "run", timeout)

    with pytest.raises(RuntimeError, match="timed out after 30s"):
        kubectl.exec_command_checked("kubectl exec pod -- command", timeout=30)
