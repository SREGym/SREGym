import os
import subprocess
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from sregym.service import judge_runtime as runtime
from sregym.service.container_runner import ContainerRunner


@pytest.mark.parametrize("failure", [False, True])
@pytest.mark.parametrize("backend", ["cursor", "codex"])
@pytest.mark.parametrize("force_build", [False, True])
def test_judge_launch_lookup_and_cleanup_use_one_trusted_engine(monkeypatch, tmp_path, failure, backend, force_build):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SREGYM_ROOTLESS_WORKLOAD", "1")
    monkeypatch.setenv("SREGYM_TRUSTED_DOCKER_HOST", "unix:///trusted.sock")
    monkeypatch.setenv("DOCKER_HOST", "unix:///workload.sock")
    monkeypatch.setenv("CURSOR_API_KEY", "dummy-test-only")
    monkeypatch.delenv("SREGYM_JUDGE_BRIDGE_URL", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "selected-codex"))
    auth = tmp_path / "selected-codex/auth.json"
    auth.parent.mkdir()
    auth.write_text('{"tokens": {"access_token": "dummy-test-only"}}')
    configurations = []
    real_runner = ContainerRunner

    def create_runner(config):
        configurations.append(config)
        return real_runner(config)

    monkeypatch.setattr(runtime, "ContainerRunner", create_runner)
    monkeypatch.setattr(ContainerRunner, "ensure_image_exists", Mock())
    monkeypatch.setattr(ContainerRunner, "build_image", Mock())
    stop = Mock()
    monkeypatch.setattr(ContainerRunner, "stop_container", stop)
    proc = Mock()
    launch = Mock(return_value=proc)
    monkeypatch.setattr(runtime.subprocess, "Popen", launch)
    bridge = Mock(return_value="http://127.0.0.1:41001/v1")
    monkeypatch.setattr(runtime, "_wait_for_bridge", bridge)
    with (
        pytest.raises(RuntimeError) if failure else nullcontext(),
        runtime.managed_judge_backend(backend, force_build=force_build),
    ):
        command = launch.call_args.args[0]
        assert command[:3] == ["docker", "--host", "unix:///trusted.sock"]
        assert not configurations[0].isolate_mounts
        assert configurations[0].codex_auth == ("shared" if backend == "codex" else "none")
        if backend == "codex":
            assert f"{auth}:/root/.codex/auth.json:rw" in command
        name = command[command.index("--name") + 1]
        bridge.assert_called_once_with(
            proc, name, configurations[0].logs_path / "container.log", docker_host="unix:///trusted.sock"
        )
        monkeypatch.setenv("DOCKER_HOST", "unix:///unrelated.sock")
        monkeypatch.setenv("SREGYM_TRUSTED_DOCKER_HOST", "unix:///changed.sock")
        if failure:
            raise RuntimeError("benchmark interrupted")
    stop.assert_called_once_with(name, docker_host="unix:///trusted.sock")
    proc.wait.assert_called_once_with(timeout=5)
    assert ContainerRunner.build_image.call_count == int(force_build)
    assert ContainerRunner.ensure_image_exists.call_count == int(not force_build)
    assert "SREGYM_JUDGE_BRIDGE_URL" not in os.environ


def test_selected_engine_startup_failure_still_restores_url_and_cleans_up(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SREGYM_ROOTLESS_WORKLOAD", "1")
    monkeypatch.setenv("SREGYM_TRUSTED_DOCKER_HOST", "unix:///trusted.sock")
    monkeypatch.setenv("DOCKER_HOST", "unix:///workload.sock")
    monkeypatch.setenv("CURSOR_API_KEY", "dummy-test-only")
    monkeypatch.setenv("SREGYM_JUDGE_BRIDGE_URL", "http://127.0.0.1:42000/v1")
    monkeypatch.setattr(ContainerRunner, "ensure_image_exists", Mock())
    stop = Mock()
    monkeypatch.setattr(ContainerRunner, "stop_container", stop)
    proc = Mock()
    launch = Mock(return_value=proc)
    monkeypatch.setattr(runtime.subprocess, "Popen", launch)
    bridge = Mock(side_effect=RuntimeError("bridge startup failed"))
    monkeypatch.setattr(runtime, "_wait_for_bridge", bridge)
    with pytest.raises(RuntimeError, match="bridge startup failed"), runtime.managed_judge_backend("cursor"):
        pytest.fail("startup failure cannot yield")
    command = launch.call_args.args[0]
    assert command[:3] == ["docker", "--host", "unix:///trusted.sock"]
    stop.assert_called_once_with(command[command.index("--name") + 1], docker_host="unix:///trusted.sock")
    assert bridge.call_args.kwargs == {"docker_host": "unix:///trusted.sock"}
    proc.wait.assert_called_once_with(timeout=5)
    assert os.environ["SREGYM_JUDGE_BRIDGE_URL"] == "http://127.0.0.1:42000/v1"


def test_rootless_judge_rejects_missing_trusted_endpoint_before_launch(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SREGYM_ROOTLESS_WORKLOAD", "1")
    monkeypatch.delenv("SREGYM_TRUSTED_DOCKER_HOST", raising=False)
    monkeypatch.setenv("CURSOR_API_KEY", "dummy-test-only")
    launch = Mock()
    monkeypatch.setattr(runtime.subprocess, "Popen", launch)
    with (
        pytest.raises(ValueError, match="explicit trusted Docker endpoint"),
        runtime.managed_judge_backend("cursor"),
    ):
        pytest.fail("missing endpoint must fail before yield")
    launch.assert_not_called()


def test_bridge_port_lookup_uses_selected_engine_and_bypasses_http_proxy(monkeypatch, tmp_path):
    run = Mock(return_value=subprocess.CompletedProcess([], 0, stdout="127.0.0.1:41001\n"))
    monkeypatch.setattr(runtime.subprocess, "run", run)
    response = Mock()
    response.__enter__ = Mock(return_value=SimpleNamespace(status=200))
    response.__exit__ = Mock(return_value=False)
    opener = Mock()
    opener.open.return_value = response
    factory = Mock(return_value=opener)
    monkeypatch.setattr(runtime, "build_opener", factory)
    process = Mock()
    process.poll.return_value = None
    assert (
        runtime._wait_for_bridge(process, "owned-judge", tmp_path / "log", docker_host="unix:///trusted.sock")
        == "http://127.0.0.1:41001/v1"
    )
    assert run.call_args.args[0] == ["docker", "--host", "unix:///trusted.sock", "port", "owned-judge", "4100/tcp"]
    assert factory.call_args.args[0].proxies == {}
    opener.open.assert_called_once_with("http://127.0.0.1:41001/health", timeout=1)
