import json
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

from sregym.service.container_runner import ContainerConfig, ContainerRunner, ExecInput
from sregym.service.internet_policy import InternetPolicy


@pytest.mark.parametrize("cached", [False, True])
def test_public_context_is_fresh_private_and_uses_the_selected_engine(monkeypatch, cached):
    monkeypatch.setenv("DOCKER_HOST", "unix:///unrelated.sock")
    runner = ContainerRunner(ContainerConfig(docker_host="unix:///selected.sock", isolate_mounts=True))
    run = Mock(return_value=subprocess.CompletedProcess([], 0 if cached else 1))
    monkeypatch.setattr(subprocess, "run", run)
    contexts = []

    def build(command, operation):
        assert operation == "prepare public agent runtime"
        assert command[:4] == ["docker", "--host", "unix:///selected.sock", "build"]
        assert "--build-arg" not in command
        context = Path(command[-1])
        contexts.append(context)
        assert context.name == "context"
        assert (context / "runtime/incident_runtime/paths.py").exists()
        assert not (context / "runtime/incident_runtime/conductor").exists()
        assert not (context / "runtime/llm_backend/judge_bridge.py").exists()
        assert not (context / "runtime/clients/cursor/judge_bridge.py").exists()
        assert b"session_id" in (context / "runtime/incident_runtime/session.py").read_bytes()

    runner._run_docker_checked = Mock(side_effect=build)
    runner._prepare_public_runtime()
    assert runner.config.image.startswith("incident-agent:runtime-")
    assert runner._volumes.image == runner.config.image
    assert runner.runtime_root == "/opt/runtime"
    assert runner._run_docker_checked.call_count == int(not cached)
    assert all(not context.exists() for context in contexts)
    assert run.call_args.args[0][:4] == ["docker", "--host", "unix:///selected.sock", "image"]


def test_failed_public_build_keeps_original_image_and_releases_context(monkeypatch):
    runner = ContainerRunner(ContainerConfig(isolate_mounts=True, docker_host="unix:///selected.sock"))
    original = runner.config.image
    monkeypatch.setattr(subprocess, "run", Mock(return_value=subprocess.CompletedProcess([], 1)))
    paths = []

    def fail(command, _operation):
        paths.append(Path(command[-1]))
        raise RuntimeError("public build failed")

    runner._run_docker_checked = Mock(side_effect=fail)
    with pytest.raises(RuntimeError, match="public build failed"):
        runner._prepare_public_runtime()
    assert runner.config.image == runner._volumes.image == original
    assert runner.runtime_root == "/opt/sregym"
    assert all(not path.exists() for path in paths)


@pytest.mark.parametrize("public", [False, True])
@pytest.mark.parametrize("image", ["custom.example/agent:v1", "incident-agent:runtime-" + "a" * 64])
def test_custom_image_layout_preserves_legacy_or_selects_neutral_paths(monkeypatch, tmp_path, public, image):
    label = {"io.incident.runtime.root": "/opt/runtime"} if public else {}
    inspection = subprocess.CompletedProcess([], 0, stdout=json.dumps([{"Config": {"Labels": label}}]))
    run = Mock(return_value=inspection)
    monkeypatch.setattr(subprocess, "run", run)
    apps = tmp_path / "apps"
    (apps / "wrk2").mkdir(parents=True)
    runner = ContainerRunner(
        ContainerConfig(
            image=image,
            sregym_apps_path=apps,
            sregym_app_subdirs=["wrk2"],
            codex_auth="none",
            forward_host_credentials=False,
            internet_policy=InternetPolicy.from_mode("open"),
        )
    )
    runner.ensure_image_exists()
    run.assert_called_once()
    root = "/opt/runtime" if public else "/opt/sregym"
    assert runner.runtime_root == root
    command = runner.build_composite_command("install.sh", "v1", "true", capture_logs=False)
    assert f"{root}/install-scripts/install.sh" in command
    env = runner._build_env_vars({"SREGYM_ARTIFACT_ID": "artifact", "SREGYM_PROBLEM_ID": "problem"})
    if public:
        assert env["RUN_ARTIFACT_ID"] == "artifact" and env["PROBLEM_ID"] == "problem"
        assert not any(key.startswith("SREGYM") for key in env)
    else:
        assert env["SREGYM_ARTIFACT_ID"] == "artifact" and env["SREGYM_PROBLEM_ID"] == "problem"
    request = ExecInput(command="true", label="fixture")
    docker = runner.build_docker_command(request)
    app_name = "applications" if public else "SREGym-applications"
    assert f"{(apps / 'wrk2').resolve()}:{root}/{app_name}/wrk2:ro" in docker
    assert request.container_name.startswith("agent-" if public else "sregym-")


def test_generated_judge_image_is_derived_on_workload_engine_without_mutable_pull(monkeypatch):
    inventories = {"unix:///trusted.sock": {}, "unix:///workload.sock": {}}
    commands = []

    def docker(command, **_kwargs):
        commands.append(command)
        assert command[:2] == ["docker", "--host"]
        endpoint = command[2]
        if command[3:5] == ["image", "inspect"]:
            image = command[5]
            metadata = inventories[endpoint].get(image)
            return subprocess.CompletedProcess(
                command, 0 if metadata else 1, stdout=json.dumps([metadata]) if metadata else ""
            )
        assert command[3] == "build", "Generated local runtime tags must not be pulled from a registry"
        tag = command[command.index("--tag") + 1]
        inventories[endpoint][tag] = {"Config": {"Labels": {"io.incident.runtime.root": "/opt/runtime"}}}
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(subprocess, "run", docker)
    trusted = ContainerRunner(ContainerConfig(docker_host="unix:///trusted.sock", isolate_mounts=False))
    trusted._prepare_public_runtime()
    selected = trusted.config.image
    workload = ContainerRunner(
        ContainerConfig(image=selected, docker_host="unix:///workload.sock", isolate_mounts=True)
    )
    workload.ensure_image_exists()
    assert trusted.config.image == workload.config.image == selected
    assert trusted.runtime_root == workload.runtime_root == "/opt/runtime"
    assert workload._volumes.image == selected
    assert selected in inventories["unix:///trusted.sock"] and selected in inventories["unix:///workload.sock"]
    assert len([command for command in commands if command[3] == "build"]) == 2


def test_unknown_generated_looking_custom_image_keeps_registry_resolution(monkeypatch):
    image = "incident-agent:runtime-" + "0" * 64
    run = Mock(
        side_effect=[
            subprocess.CompletedProcess([], 1),
            subprocess.CompletedProcess([], 0),
            subprocess.CompletedProcess([], 0, stdout='[{"Config": {"Labels": {}}}]'),
        ]
    )
    monkeypatch.setattr(subprocess, "run", run)
    runner = ContainerRunner(ContainerConfig(image=image))
    runner.ensure_image_exists()
    assert run.call_args_list[1].args[0] == ["docker", "pull", image]
    assert runner.config.image == image and runner.runtime_root == "/opt/sregym"


def test_switching_a_prepared_runner_to_a_legacy_custom_image_clears_public_layout(monkeypatch):
    runner = ContainerRunner(ContainerConfig(image="custom.example/legacy:v1"))
    runner._public_runtime = True
    inspection = subprocess.CompletedProcess([], 0, stdout='[{"Config": {"Labels": {}}}]')
    monkeypatch.setattr(subprocess, "run", Mock(return_value=inspection))
    runner.ensure_image_exists()
    assert runner.runtime_root == "/opt/sregym"
