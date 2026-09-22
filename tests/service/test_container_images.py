import subprocess
from unittest.mock import Mock

import pytest

from sregym.service.container_runner import DEFAULT_AGENT_IMAGE, LOCAL_AGENT_IMAGE, ContainerConfig, ContainerRunner


def test_cached_image_needs_no_pull_or_build(monkeypatch):
    run = Mock(return_value=subprocess.CompletedProcess([], 0))
    monkeypatch.setattr(subprocess, "run", run)
    runner = ContainerRunner(ContainerConfig())
    runner.ensure_image_exists()
    run.assert_called_once_with(["docker", "image", "inspect", DEFAULT_AGENT_IMAGE], capture_output=True)


@pytest.mark.parametrize("image", [DEFAULT_AGENT_IMAGE, "example.org/custom-agent:v1"])
def test_missing_release_is_pulled_not_rebuilt(monkeypatch, image):
    run = Mock(side_effect=[subprocess.CompletedProcess([], 1), subprocess.CompletedProcess([], 0)])
    monkeypatch.setattr(subprocess, "run", run)
    runner = ContainerRunner(ContainerConfig(image=image))
    runner.ensure_image_exists()
    assert run.call_args.args[0] == ["docker", "pull", image]


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
