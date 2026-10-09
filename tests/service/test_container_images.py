import subprocess
from unittest.mock import Mock

import pytest

from sregym.service.container_runner import DEFAULT_AGENT_IMAGE, LOCAL_AGENT_IMAGE, ContainerConfig, ContainerRunner


def test_cached_dependency_and_public_runtime_need_no_pull_or_build(monkeypatch):
    run = Mock(return_value=subprocess.CompletedProcess([], 0))
    monkeypatch.setattr(subprocess, "run", run)
    runner = ContainerRunner(ContainerConfig())
    runner.ensure_image_exists()
    assert run.call_count == 2
    assert run.call_args_list[0].args[0] == ["docker", "image", "inspect", DEFAULT_AGENT_IMAGE]
    assert runner.config.image.startswith("incident-agent:runtime-")
    assert run.call_args.args[0] == ["docker", "image", "inspect", runner.config.image]


@pytest.mark.parametrize("image", [DEFAULT_AGENT_IMAGE, "example.org/custom-agent:v1"])
def test_missing_release_is_pulled_not_rebuilt(monkeypatch, image):
    run = Mock(
        side_effect=[
            subprocess.CompletedProcess([], 1),
            subprocess.CompletedProcess([], 0),
            subprocess.CompletedProcess([], 0),
        ]
    )
    monkeypatch.setattr(subprocess, "run", run)
    runner = ContainerRunner(ContainerConfig(image=image))
    runner._prepare_public_runtime = Mock()
    runner.ensure_image_exists()
    assert run.call_args_list[1].args[0] == ["docker", "pull", image]
    assert runner._prepare_public_runtime.call_count == int(image == DEFAULT_AGENT_IMAGE)


@pytest.mark.parametrize("status", [0, 1])
def test_public_runtime_build_preserves_dependency_identity_and_only_selects_after_success(monkeypatch, status):
    run = Mock(
        side_effect=[
            subprocess.CompletedProcess([], 0),
            subprocess.CompletedProcess([], 1),
            subprocess.CompletedProcess([], status, "", "build failed"),
        ]
    )
    monkeypatch.setattr(subprocess, "run", run)
    runner = ContainerRunner(ContainerConfig())
    if status:
        with pytest.raises(RuntimeError, match="build failed"):
            runner.ensure_image_exists()
        assert runner.config.image == DEFAULT_AGENT_IMAGE
    else:
        runner.ensure_image_exists()
        assert runner.config.image.startswith("incident-agent:runtime-")
    command = run.call_args.args[0]
    assert command[:2] == ["docker", "build"]
    assert f"BASE_IMAGE={DEFAULT_AGENT_IMAGE}" in command
    assert command[command.index("--tag") + 1] != DEFAULT_AGENT_IMAGE


def test_legacy_host_artifact_setting_is_forwarded_with_neutral_name():
    runner = ContainerRunner(ContainerConfig(forward_host_credentials=False))
    env = runner._build_env_vars({"SREGYM_ARTIFACT_ID": "anon_123", "AGENT_LOGS_DIR": "/logs"})
    assert env["RUN_ARTIFACT_ID"] == "anon_123"
    assert env["AGENT_LOGS_DIR"] == "/logs"
    assert not any("sregym" in key.lower() for key in env)


@pytest.mark.parametrize("modern", [False, True])
def test_explicit_custom_image_retains_its_runtime_contract(monkeypatch, modern):
    metadata = (
        '[{"Config": {"Labels": {"io.incident.runtime.root": "/opt/runtime"}}}]' if modern else '[{"Config": {}}]'
    )
    run = Mock(return_value=subprocess.CompletedProcess([], 0, metadata))
    monkeypatch.setattr(subprocess, "run", run)
    runner = ContainerRunner(ContainerConfig(image="custom-agent:v1", forward_host_credentials=False))
    runner.ensure_image_exists()
    assert runner.config.image == "custom-agent:v1"
    assert runner.runtime_root == ("/opt/runtime" if modern else "/opt/sregym")
    command = runner.build_composite_command("install-codex.sh", None, "python -m clients.codex.driver")
    assert f"{runner.runtime_root}/install-scripts/install-codex.sh" in command
    env = runner._build_env_vars({"SREGYM_ARTIFACT_ID": "anon_123"})
    assert env["RUN_ARTIFACT_ID" if modern else "SREGYM_ARTIFACT_ID"] == "anon_123"
    run.assert_called_once()


def test_failed_pull_does_not_silently_build_different_code(monkeypatch):
    run = Mock(side_effect=[subprocess.CompletedProcess([], 1), subprocess.CompletedProcess([], 1, "", "denied")])
    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(RuntimeError, match="denied"):
        ContainerRunner(ContainerConfig()).ensure_image_exists()
    assert run.call_count == 2


def test_local_development_tag_still_auto_builds(monkeypatch):
    monkeypatch.setattr(subprocess, "run", Mock(return_value=subprocess.CompletedProcess([], 1)))
    runner = ContainerRunner(ContainerConfig(image=LOCAL_AGENT_IMAGE))
    runner.build_image = Mock()
    runner.ensure_image_exists()
    runner.build_image.assert_called_once_with()


@pytest.mark.parametrize("status", [0, 1])
def test_explicit_rebuild_selects_local_image_only_after_success(monkeypatch, status):
    run = Mock(return_value=subprocess.CompletedProcess([], status))
    monkeypatch.setattr(subprocess, "run", run)
    runner = ContainerRunner(ContainerConfig())
    if status:
        with pytest.raises(RuntimeError, match="Failed to build"):
            runner.build_image()
        assert runner.config.image == DEFAULT_AGENT_IMAGE
    else:
        runner.build_image()
        assert runner.config.image == LOCAL_AGENT_IMAGE
    assert run.call_args.kwargs["env"]["SREGYM_AGENT_IMAGE"] == LOCAL_AGENT_IMAGE
    assert run.call_args.args[0][0] == "bash"


def test_custom_digest_cannot_be_rebuilt(monkeypatch):
    run = Mock()
    monkeypatch.setattr(subprocess, "run", run)
    runner = ContainerRunner(ContainerConfig(image="example.org/agent@sha256:abc"))
    with pytest.raises(ValueError, match="digest-pinned"):
        runner.build_image()
    run.assert_not_called()
