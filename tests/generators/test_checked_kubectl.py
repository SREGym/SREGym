import subprocess

import pytest

from sregym.service.kubectl import KubeCtl


def test_checked_command_returns_stdout(monkeypatch):
    completed = subprocess.CompletedProcess(args="kubectl version", returncode=0, stdout=b"ok\n", stderr=b"")
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: completed)

    assert KubeCtl().exec_command_checked("kubectl version") == "ok\n"


def test_checked_command_raises_on_nonzero_exit(monkeypatch):
    def fail(*args, **kwargs):
        raise subprocess.CalledProcessError(1, "kubectl apply", stderr=b"apply rejected")

    monkeypatch.setattr(subprocess, "run", fail)

    with pytest.raises(RuntimeError, match="apply rejected"):
        KubeCtl().exec_command_checked("kubectl apply")
