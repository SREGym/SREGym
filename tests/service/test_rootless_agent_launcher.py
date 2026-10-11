import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from sregym.agent_launcher import AgentLauncher, AgentProcess
from sregym.agent_registry import AgentRegistration
from sregym.service.container_runner import ContainerRunner
from sregym.service.internet_policy import InternetPolicy


@pytest.mark.parametrize("registration_isolated, launcher_containers", [(False, True), (True, False)])
def test_rootless_host_execution_denied_before_prepare_or_launch(
    monkeypatch, registration_isolated, launcher_containers
):
    monkeypatch.setenv("SREGYM_ROOTLESS_WORKLOAD", "1")
    launcher = AgentLauncher()
    launcher._use_containers = launcher_containers
    launcher.set_internet_policy(InternetPolicy.from_mode("open"))
    launcher.enable_container_isolation = Mock()
    popen = Mock()
    monkeypatch.setattr("sregym.agent_launcher.subprocess.Popen", popen)
    reg = AgentRegistration("fixture", kickoff_command="true", container_isolation=registration_isolated)
    with pytest.raises(RuntimeError, match="workload boundary"):
        launcher.prepare_agent(reg)
    with pytest.raises(RuntimeError, match="trusted host user"):
        asyncio.run(launcher.ensure_started(reg))
    launcher.enable_container_isolation.assert_not_called()
    popen.assert_not_called()


def test_rootless_existing_trusted_runner_denied_before_preflight_or_start(monkeypatch):
    monkeypatch.setenv("SREGYM_ROOTLESS_WORKLOAD", "1")
    launcher = AgentLauncher()
    runner = Mock(config=SimpleNamespace(isolate_mounts=False))
    launcher._container_runner = runner
    reg = AgentRegistration("fixture", kickoff_command="true")
    with pytest.raises(RuntimeError, match="isolated workload runner"):
        launcher.prepare_agent(reg)
    runner.prepare_agent_tools.assert_not_called()
    runner.close.assert_called_once_with()
    launcher._container_runner = runner
    launcher._start_containerized = Mock()
    with pytest.raises(RuntimeError, match="isolated workload runner"):
        asyncio.run(launcher.ensure_started(reg))
    launcher._start_containerized.assert_not_called()


def test_rootless_cannot_reuse_an_existing_host_process(monkeypatch):
    monkeypatch.setenv("SREGYM_ROOTLESS_WORKLOAD", "1")
    launcher = AgentLauncher()
    proc = Mock(returncode=None)
    launcher._procs["fixture"] = AgentProcess("fixture", proc)
    with pytest.raises(RuntimeError, match="reuse a trusted host agent"):
        asyncio.run(launcher.ensure_started(AgentRegistration("fixture", kickoff_command="true")))
    proc.poll.assert_not_called()


def test_ordinary_host_agent_still_launches_when_rootless_is_disabled(monkeypatch):
    monkeypatch.delenv("SREGYM_ROOTLESS_WORKLOAD", raising=False)
    launcher = AgentLauncher()
    launcher.set_internet_policy(InternetPolicy.from_mode("open"))
    proc = Mock(pid=1234, stdout=None, returncode=None)
    popen = Mock(return_value=proc)
    monkeypatch.setattr("sregym.agent_launcher.subprocess.Popen", popen)
    reg = AgentRegistration("fixture", kickoff_command="ordinary-fixture", container_isolation=False)
    result = asyncio.run(launcher.ensure_started(reg))
    assert result.proc is proc and result.pgid == 1234 and result.container_name is None
    assert popen.call_args.args == ("ordinary-fixture",)
    assert popen.call_args.kwargs["start_new_session"] is True


@pytest.mark.parametrize("completed", [False, True])
def test_cleanup_keeps_the_launch_engine_and_failed_ownership_retryable(monkeypatch, completed):
    monkeypatch.setenv("DOCKER_HOST", "unix:///unrelated.sock")
    launcher = AgentLauncher()
    proc = Mock(returncode=0 if completed else None)
    process = AgentProcess("fixture", proc)
    process.container_name = "owned-container"
    process.docker_host = "unix:///launch-engine.sock"
    process.isolate_mounts = True
    launcher._procs[process.name] = process
    stop = Mock(side_effect=[RuntimeError("owned cleanup failed"), None])
    monkeypatch.setattr(ContainerRunner, "stop_container", stop)
    launcher._cleanup_container_runner_tmps = Mock()
    launcher._wait_for_process_exit = Mock()
    with pytest.raises(RuntimeError, match="owned cleanup failed"):
        launcher.cleanup_agent(process.name)
    assert launcher._procs[process.name] is process
    launcher._cleanup_container_runner_tmps.assert_not_called()
    launcher.cleanup_agent(process.name)
    assert process.name not in launcher._procs
    for call in stop.call_args_list:
        assert call.args == ("owned-container",)
        assert call.kwargs == {"timeout": 5, "docker_host": "unix:///launch-engine.sock", "strict": True}
    assert launcher._wait_for_process_exit.call_count == (0 if completed else 1)


@pytest.mark.parametrize("isolated", [False, True])
def test_container_process_records_endpoint_and_isolation_at_launch(monkeypatch, isolated, tmp_path):
    monkeypatch.delenv("SREGYM_ROOTLESS_WORKLOAD", raising=False)
    monkeypatch.setenv("AGENT_LOGS_DIR", str(tmp_path))
    launcher = AgentLauncher()
    proc = Mock(stdout=None)
    runner = Mock(config=SimpleNamespace(env_vars={}, isolate_mounts=isolated, docker_host="unix:///selected.sock"))
    runner.build_composite_command.return_value = "true"

    def run_async(request):
        request.container_name = "owned-fixture"
        return proc

    runner.run_async.side_effect = run_async
    launcher._container_runner = runner
    process = asyncio.run(launcher._start_containerized(AgentRegistration("fixture", kickoff_command="true")))
    assert process.docker_host == "unix:///selected.sock"
    assert process.isolate_mounts is isolated
    assert process.container_name == "owned-fixture"


def test_rootless_container_registration_is_still_launchable(monkeypatch):
    monkeypatch.setenv("SREGYM_ROOTLESS_WORKLOAD", "1")
    launcher = AgentLauncher()
    launcher._container_runner = Mock(config=SimpleNamespace(isolate_mounts=True))

    async def start(reg):
        return reg

    launcher._start_containerized = Mock(side_effect=start)
    reg = AgentRegistration("fixture", kickoff_command="true")
    assert asyncio.run(launcher.ensure_started(reg)) is reg


def test_failed_runner_close_retains_its_owned_resources_for_retry():
    launcher = AgentLauncher()
    runner = Mock()
    runner.close.side_effect = [RuntimeError("owned volume cleanup failed"), None]
    launcher._container_runner = runner
    with pytest.raises(ExceptionGroup) as failure:
        launcher.cleanup_all()
    assert str(failure.value.exceptions[0]) == "owned volume cleanup failed"
    assert launcher._container_runner is runner
    launcher.cleanup_all()
    assert launcher._container_runner is None
    assert runner.close.call_count == 2


def test_cleanup_attempts_other_agents_and_runner_after_one_agent_failure():
    launcher = AgentLauncher()
    first, second = object(), object()
    launcher._procs = {"first": first, "second": second}
    runner = Mock()
    runner.close.side_effect = RuntimeError("owned runner cleanup failed")
    launcher._container_runner = runner

    def cleanup(name, timeout):
        if name == "first":
            raise RuntimeError("first cleanup failed")
        del launcher._procs[name]

    launcher.cleanup_agent = Mock(side_effect=cleanup)
    with pytest.raises(ExceptionGroup) as failure:
        launcher.cleanup_all()
    assert [str(exc) for exc in failure.value.exceptions] == ["first cleanup failed", "owned runner cleanup failed"]
    assert launcher._procs == {"first": first}
    assert launcher._container_runner is runner
    assert [call.args[0] for call in launcher.cleanup_agent.call_args_list] == ["first", "second"]
    runner.close.assert_called_once_with()


def test_cleanup_releases_matching_owner_and_waits_for_other_active_output(monkeypatch):
    launcher = AgentLauncher()
    runner = Mock(
        config=SimpleNamespace(isolate_mounts=True),
        _owned_containers={"first-container": "unix:///selected.sock", "second-container": "unix:///selected.sock"},
    )
    launcher._container_runner = runner
    for name in ("first", "second"):
        process = AgentProcess(name, Mock(returncode=0))
        process.container_name = name + "-container"
        process.docker_host = "unix:///selected.sock"
        process.isolate_mounts = True
        launcher._procs[name] = process
    monkeypatch.setattr(ContainerRunner, "stop_container", Mock())
    launcher.cleanup_agent("first")
    assert runner._owned_containers == {"second-container": "unix:///selected.sock"}
    runner.cleanup_credential_tmps.assert_not_called()
    launcher.cleanup_agent("second")
    assert not runner._owned_containers
    runner.cleanup_credential_tmps.assert_called_once_with()


@pytest.mark.parametrize("failure", [False, True])
def test_completed_isolated_restart_cleans_old_owner_before_replacement(monkeypatch, failure):
    monkeypatch.setenv("SREGYM_ROOTLESS_WORKLOAD", "1")
    launcher = AgentLauncher()
    old = AgentProcess("fixture", Mock(returncode=0))
    old.isolate_mounts = True
    launcher._procs["fixture"] = old
    events = []

    def cleanup(name):
        events.append("cleanup")
        if failure:
            raise RuntimeError("old owner cleanup failed")
        del launcher._procs[name]

    async def start(reg):
        events.append("start")
        assert "fixture" not in launcher._procs
        return reg

    launcher.cleanup_agent = Mock(side_effect=cleanup)
    launcher._start_containerized = Mock(side_effect=start)
    reg = AgentRegistration("fixture", kickoff_command="true")
    if failure:
        with pytest.raises(RuntimeError, match="old owner cleanup failed"):
            asyncio.run(launcher.ensure_started(reg))
        assert launcher._procs["fixture"] is old
        assert events == ["cleanup"]
    else:
        assert asyncio.run(launcher.ensure_started(reg)) is reg
        assert events == ["cleanup", "start"]
