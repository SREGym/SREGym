import json
import os
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from llm_backend.init_backend import get_llm_backend_for_judge
from sregym.service import judge_runtime as runtime
from sregym.service.container_runner import ContainerRunner


@pytest.fixture(autouse=True)
def environment(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for key in ("CODEX_HOME", "SREGYM_JUDGE_BRIDGE_URL"):
        monkeypatch.delenv(key, raising=False)
    for key, value in {
        "CURSOR_API_KEY": "cursor-test",
        "JUDGE_API_BASE": "https://existing.example/v1",
        "JUDGE_API_KEY": "existing-key",
        "OPENAI_API_KEY": "unrelated-key",
        "AWS_PROFILE": "unrelated-profile",
    }.items():
        monkeypatch.setenv(key, value)
    (tmp_path / ".aws").mkdir()
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex/auth.json").write_text('{"tokens": {"access_token": "other-profile"}}')


@pytest.fixture
def docker(monkeypatch):
    for method in ("build_image", "ensure_image_exists", "stop_container"):
        monkeypatch.setattr(ContainerRunner, method, Mock())
    process = Mock()
    monkeypatch.setattr(runtime.subprocess, "Popen", Mock(return_value=process))
    monkeypatch.setattr(runtime, "_wait_for_bridge", Mock(return_value="http://127.0.0.1:41001/v1"))
    return process


@pytest.mark.parametrize(
    "error,startup,force_build",
    [(None, False, False), (RuntimeError, False, True), (KeyboardInterrupt, False, False), (RuntimeError, True, False)],
)
@pytest.mark.parametrize("previous_url", [None, "http://127.0.0.1:42000/v1"])
def test_lifecycle_restores_settings_and_cleans_up(
    docker, monkeypatch, tmp_path, error, startup, force_build, previous_url
):
    if previous_url:
        monkeypatch.setenv("SREGYM_JUDGE_BRIDGE_URL", previous_url)
    if startup:
        runtime._wait_for_bridge.side_effect = error("startup failed")
    with (
        pytest.raises(error) if error else nullcontext(),
        runtime.managed_judge_backend("cursor", force_build=force_build),
    ):
        assert not startup
        assert os.environ["SREGYM_JUDGE_BRIDGE_URL"] == "http://127.0.0.1:41001/v1"
        if error:
            raise error("benchmark interrupted")
    command = runtime.subprocess.Popen.call_args.args[0]
    bridge = next((tmp_path / "logs").glob("judge-cursor-*/judge_bridge.py"))
    assert (
        bridge.read_bytes()
        == (Path(runtime.__file__).resolve().parents[2] / "llm_backend/judge_bridge.py").read_bytes()
    )
    assert f"{bridge.parent}:/logs" in command and "python /logs/judge_bridge.py" in command[-1]
    assert "CURSOR_API_KEY=cursor-test" in command and "127.0.0.1::4100" in command
    assert not any("unrelated" in arg or "/root/.aws" in arg or "/root/.codex" in arg for arg in command)
    ContainerRunner.stop_container.assert_called_once_with(command[command.index("--name") + 1])
    assert ContainerRunner.build_image.call_count == int(force_build)
    assert ContainerRunner.ensure_image_exists.call_count == int(not force_build)
    docker.wait.assert_called_once()
    assert os.environ.get("SREGYM_JUDGE_BRIDGE_URL") == previous_url
    assert os.environ["JUDGE_API_BASE"] == "https://existing.example/v1"
    assert os.environ["JUDGE_API_KEY"] == "existing-key"


def test_api_mode_preserves_manual_endpoint_without_starting_container(docker):
    with runtime.managed_judge_backend():
        backend = get_llm_backend_for_judge(model_name="openai/auto")
        assert backend.api_base == "https://existing.example/v1" and backend.api_key == "existing-key"
    runtime.subprocess.Popen.assert_not_called()


@pytest.mark.parametrize("backend", ["api", "cursor"])
@pytest.mark.parametrize("external", [False, True])
def test_main_selects_backend_and_skips_it_for_external_harness(docker, monkeypatch, backend, external):
    import main

    run = Mock(return_value="finished")
    monkeypatch.setattr(main, "init_logger", lambda: None)
    monkeypatch.setattr(main, "_run_benchmark", run)
    args = SimpleNamespace(judge_backend=backend, use_external_harness=external, force_build=False)
    assert main.main(args) == "finished"
    run.assert_called_once_with(args, judge_backend="api" if external else backend)


@pytest.mark.parametrize("backend,variable", runtime.AUTH_VARIABLES.items())
def test_missing_credentials_fail_before_start(docker, monkeypatch, backend, variable):
    monkeypatch.delenv(variable, raising=False)
    with pytest.raises(ValueError, match=variable), runtime.managed_judge_backend(backend):
        pytest.fail("missing credentials must not start a benchmark")
    runtime.subprocess.Popen.assert_not_called()


@pytest.mark.parametrize("custom_home", [False, True])
@pytest.mark.parametrize(
    "auth", [{"tokens": {"access_token": "selected-profile"}}, {"OPENAI_API_KEY": "api-key"}, None]
)
def test_codex_uses_only_selected_subscription_file(docker, monkeypatch, tmp_path, custom_home, auth):
    home = tmp_path / ("selected-profile" if custom_home else ".codex")
    home.mkdir(exist_ok=True)
    if custom_home:
        monkeypatch.setenv("CODEX_HOME", str(home))
    path = home / "auth.json"
    if auth is None:
        path.unlink(missing_ok=True)
    else:
        path.write_text(json.dumps(auth))
    if auth is None or "tokens" not in auth:
        with pytest.raises(ValueError, match="subscription credentials"), runtime.managed_judge_backend("codex"):
            pytest.fail("must not use API credentials or another profile")
        runtime.subprocess.Popen.assert_not_called()
    else:
        with runtime.managed_judge_backend("codex"):
            assert f"{path}:/root/.codex/auth.json:rw" in runtime.subprocess.Popen.call_args.args[0]
        assert json.loads(path.read_text()) == auth


@pytest.mark.parametrize("model", ["local/gpt-5", "gpt-5"])
def test_cli_judge_does_not_require_or_inherit_agent_endpoint(docker, monkeypatch, model):
    import main

    monkeypatch.setenv("AGENT_API_BASE", "https://agent.example/v1")
    monkeypatch.setenv("AGENT_API_KEY", "agent-key")
    monkeypatch.delenv("JUDGE_API_BASE")
    monkeypatch.delenv("JUDGE_API_KEY")
    with runtime.managed_judge_backend("cursor"):
        main._configure_model_environment(SimpleNamespace(agent="opencode", model=model, judge_model=None))
        backend = get_llm_backend_for_judge()
        assert backend.api_base == "http://127.0.0.1:41001/v1" and backend.api_key == "dummy"
        assert "JUDGE_API_BASE" not in os.environ and "JUDGE_API_KEY" not in os.environ
