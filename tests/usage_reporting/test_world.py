import hashlib
import json
import os
import re
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

from sregym.conductor.problems import usage_reporting as usage_problem
from sregym.conductor.problems.registry import ProblemRegistry
from sregym.service.apps.smart_meter import application as world
from sregym.service.container_runner import ContainerConfig, ContainerRunner
from sregym.service.internet_policy import InternetPolicy


@pytest.fixture
def app(tmp_path, monkeypatch):
    project = tmp_path / "bundle"
    for name in (
        "world/revive.sh",
        "world/companyctl",
        "world/sregym_lifecycle.py",
        "grader/grade.py",
        "world/vm/states/usage-reporting/ready.json",
    ):
        path = project / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}")
    (project / "world/artifacts/runs").mkdir(parents=True)
    monkeypatch.setattr(world, "require_kvm", lambda: None)
    monkeypatch.setattr(world.shutil, "which", lambda _name: "/test/bin")
    monkeypatch.setattr(world, "get_container_host_bind_address", lambda: "172.17.0.1")
    return world.SmartMeter(project, state="usage-reporting", runs_dir=tmp_path / "runs")


def test_problem_sets_its_description_and_world_on_the_application(tmp_path):
    app = usage_problem.UsageReporting(world_root=tmp_path / "bundle").app
    assert app.state == "usage-reporting"
    assert app.runs_dir == (Path.home() / ".local/state/sregym/usage-worlds").resolve()
    assert app.lock == (tmp_path / "bundle/.sregym-world.lock").resolve()
    for name in ("OPENING.md", "COMPANY-TOOLS.md"):
        assert (usage_problem.ASSETS / name).read_text().strip() in app.description
    assert app.app_name == "smart-meter"


def test_application_alone_describes_smart_meter_and_selects_no_saved_world(tmp_path):
    app = world.SmartMeter(tmp_path)
    assert app.description == json.loads(world.SmartMeter.metadata_file.read_text())["Desc"]
    assert app.state is None


def test_tools_page_lists_exactly_the_declared_endpoints():
    tools = (usage_problem.ASSETS / "COMPANY-TOOLS.md").read_text()
    assert set(re.findall(r"`(http://[^`/]+)`", tools)) == set(world.SmartMeter.agent_endpoints)
    assert "127.0.0.1" not in tools
    assert "http://host.docker.internal:28311/companyctl" in tools


def test_problem_asks_the_egress_proxy_to_outlast_the_longest_console_command():
    registry = ProblemRegistry()
    assert registry.egress_idle_timeout("usage_reporting") == 3600
    # Every other problem, factory entries included, starts the proxy with mitmproxy's default.
    assert {p for p in registry.get_problem_ids() if registry.egress_idle_timeout(p) is not None} == {"usage_reporting"}


def test_filtered_allowlist_adds_exactly_the_declared_ports():
    endpoints = ProblemRegistry().agent_endpoints("usage_reporting")
    assert endpoints == world.SmartMeter.agent_endpoints
    policy = InternetPolicy.from_mode("filtered", additional_allowed_endpoints=endpoints)
    runner = ContainerRunner(ContainerConfig(internet_policy=policy, cluster_access=False))
    rules = runner._configured_egress_rules({"API_PORT": "8000"})
    # The conductor API and the declared ports; whole ports, as for --allow-agent-endpoint.
    assert {(rule.port, rule.path_prefix) for rule in rules if rule.host == "host.docker.internal"} == {
        (port, "/") for port in (8000, *world.AGENT_PORTS)
    }


def test_missing_kvm_is_refused(monkeypatch):
    monkeypatch.setattr(Path, "stat", Mock(side_effect=FileNotFoundError))
    with pytest.raises(RuntimeError, match="KVM is required.*readable/writable"):
        world.require_kvm()


def test_preflight_refuses_an_existing_world_without_changes(app, monkeypatch):
    (app.project / "world/vm/eu-parent-01.pid").write_text(str(os.getpid()))
    command = Mock()
    monkeypatch.setattr(world.subprocess, "run", command)
    with pytest.raises(RuntimeError, match="already in use.*eu-parent-01"):
        app.deploy()
    command.assert_not_called()
    assert not app.run_dir.exists()


def test_deploy_refuses_a_world_another_run_holds_without_changes(app, monkeypatch):
    command = Mock()
    monkeypatch.setattr(world.subprocess, "run", command)
    with app.lock.open("a+") as lease:
        world.fcntl.flock(lease, world.fcntl.LOCK_EX | world.fcntl.LOCK_NB)
        with pytest.raises(RuntimeError, match="already in use.*another SREGym run holds"):
            app.deploy()
    command.assert_not_called()
    assert app._lease is None
    assert not app.run_dir.exists()


def test_preflight_accepts_a_stale_pid(app):
    (app.project / "world/vm/old.pid").write_text("999999999")
    app.preflight()


def test_preflight_requires_the_saved_state(app):
    app.state = "missing"
    with pytest.raises(RuntimeError, match="'missing' is not installed"):
        app.preflight()


def test_cleanup_before_deploy_does_nothing(app, monkeypatch):
    command = Mock()
    monkeypatch.setattr(world.subprocess, "run", command)
    app.cleanup()
    command.assert_not_called()


def test_deploy_revives_records_the_start_and_cleanup_tears_down_once(app, monkeypatch):
    calls = []

    def command(argv, **kwargs):
        calls.append(argv)
        if argv[0].endswith("revive.sh"):
            app.run_dir.mkdir(parents=True)
            (app.project / "world/world-config.json").write_text(json.dumps({"probe_run": str(app.run_dir)}))
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(world.subprocess, "run", command)
    app.deploy()
    launch = json.loads((app.run_dir / "launch.json").read_text())
    assert set(launch) == {"start_monotonic", "start_host_epoch", "saved_state", "runner"}
    assert json.loads((app.project / "world/artifacts/runs/run-active.json").read_text()) == launch
    # The world also serves the agent's endpoints on the address agent containers reach the host on.
    assert calls[0] == [str(app.project / "world/revive.sh"), "usage-reporting", str(app.run_dir), "172.17.0.1"]
    assert "ready" in calls[1]
    app.cleanup()
    app.cleanup()
    assert sum("teardown" in c for c in calls) == 1
    assert app._lease is None


def test_world_commands_run_the_lifecycle_helper_shipped_with_the_world(app, monkeypatch):
    command = Mock()
    monkeypatch.setattr(world.subprocess, "run", command)
    app._command("ready", timeout=60)
    command.assert_called_once_with(
        [
            world.sys.executable,
            str(app.project / "world/sregym_lifecycle.py"),
            "ready",
            "--project",
            str(app.project),
            "--run",
            str(app.run_dir),
        ],
        check=True,
        timeout=60,
    )


def test_preflight_requires_the_lifecycle_helper(app):
    (app.project / "world/sregym_lifecycle.py").unlink()
    with pytest.raises(RuntimeError, match="bundle is missing .*sregym_lifecycle.py"):
        app.preflight()


def test_freeze_runs_the_helper_once(app, monkeypatch):
    command = Mock()
    monkeypatch.setattr(app, "_command", command)
    assert app.freeze() == app.run_dir / "end-state"
    assert app.freeze() == app.run_dir / "end-state"
    command.assert_called_once_with("freeze", timeout=600)


def leftover_world(app, run):
    run.mkdir(parents=True)
    (app.project / "world/world-config.json").write_text(json.dumps({"probe_run": str(run)}))
    command = Mock()
    app._command = command
    return command


def test_cleanup_on_a_fresh_application_tears_down_a_leftover_sregym_world(app):
    leftover = app.runs_dir / "run-earlier"
    command = leftover_world(app, leftover)
    app.cleanup()
    command.assert_called_once_with("teardown", run=leftover)


def test_cleanup_on_a_fresh_application_leaves_other_worlds_alone(app, tmp_path):
    command = leftover_world(app, tmp_path / "standalone-run")
    app.cleanup()
    torn_down = app.runs_dir / "run-done"
    leftover_world(app, torn_down)
    (torn_down / "teardown.json").write_text("{}")
    app.cleanup()
    app._command.assert_not_called()
    command.assert_not_called()


def test_cleanup_on_a_fresh_application_leaves_a_live_run_alone(app):
    command = leftover_world(app, app.runs_dir / "run-live")
    with app.lock.open("a+") as lease:
        world.fcntl.flock(lease, world.fcntl.LOCK_EX | world.fcntl.LOCK_NB)
        app.cleanup()
    command.assert_not_called()


def test_workspace_retention_omits_links_and_auth(app, tmp_path):
    app.run_dir.mkdir(parents=True)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "findings.md").write_text("findings")
    (workspace / "auth.json").write_text("private")
    (workspace / "outside").symlink_to(app.project, target_is_directory=True)
    (workspace / ".codex").mkdir()
    (workspace / ".codex/auth.json").write_text("private")
    app.retain_workspace(workspace)
    assert [p.name for p in (app.run_dir / "solver-work").iterdir()] == ["findings.md"]


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read any file")
def test_workspace_retention_skips_unreadable_agent_files(app, tmp_path):
    app.run_dir.mkdir(parents=True)
    workspace = tmp_path / "work"
    (workspace / "preserved/var/log").mkdir(parents=True)
    (workspace / "findings.md").write_text("ok")
    root_only = workspace / "preserved/var/log/kern.log.2"
    root_only.write_text("root only")
    root_only.chmod(0)
    app.retain_workspace(workspace)
    assert (app.run_dir / "solver-work/findings.md").read_text() == "ok"
    skipped = json.loads((app.run_dir / "solver-work-skipped.json").read_text())
    assert skipped == {"unreadable": ["preserved/var/log/kern.log.2"], "too_large": []}


def test_workspace_retention_skips_large_data_dumps(app, tmp_path, monkeypatch):
    app.run_dir.mkdir(parents=True)
    workspace = tmp_path / "work"
    workspace.mkdir()
    (workspace / "final-answer.md").write_text("done")
    (workspace / "kafka-copy.tar").write_bytes(b"x" * 2048)
    monkeypatch.setattr(world, "RETAINED_FILE_BYTES", 1024)
    app.retain_workspace(workspace)
    assert [p.name for p in (app.run_dir / "solver-work").iterdir()] == ["final-answer.md"]
    skipped = json.loads((app.run_dir / "solver-work-skipped.json").read_text())
    assert skipped == {"unreadable": [], "too_large": ["kafka-copy.tar"]}


def test_failed_deploy_cleans_up_and_releases_the_lock(app, monkeypatch):
    calls = []

    def command(argv, **kwargs):
        calls.append(argv)
        if argv[0].endswith("revive.sh"):
            app.run_dir.mkdir(parents=True)
            (app.project / "world/world-config.json").write_text(json.dumps({"probe_run": str(app.run_dir)}))
        if "ready" in argv:
            raise subprocess.CalledProcessError(1, argv)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(world.subprocess, "run", command)
    with pytest.raises(subprocess.CalledProcessError):
        app.deploy()
    assert sum("teardown" in c for c in calls) == 1
    assert app._lease is None
    assert not (app.run_dir / "launch.json").exists()


def missing_world(app, data):
    (app.states_dir / "usage-reporting/ready.json").unlink()
    app.package = {
        "url": "https://example.test/world.tar.zst",
        "sha256": hashlib.sha256(data).hexdigest(),
        "size": "7 B",
    }
    calls = []

    def command(argv, **kwargs):
        calls.append(argv)
        if argv[0] == "curl":
            Path(argv[argv.index("-o") + 1]).write_bytes(b"package")
        if "install" in argv:
            (app.states_dir / "usage-reporting/ready.json").write_text("{}")
        if argv[0].endswith("revive.sh"):
            app.run_dir.mkdir(parents=True)
            (app.project / "world/world-config.json").write_text(json.dumps({"probe_run": str(app.run_dir)}))
        return subprocess.CompletedProcess(argv, 0)

    return calls, command


def test_first_deploy_downloads_checks_and_installs_the_saved_world(app, monkeypatch):
    calls, command = missing_world(app, b"package")
    monkeypatch.setattr(world.subprocess, "run", command)
    app.preflight()  # a missing world with a package to install is not refused
    app.deploy()
    download = app.runs_dir / "world.tar.zst"
    assert calls[0][0] == "curl" and calls[0][-1] == "https://example.test/world.tar.zst"
    assert calls[1][-2:] == ["install", str(download)]
    assert calls[2][0].endswith("revive.sh")
    assert not download.exists()
    app.cleanup()
    calls.clear()
    app.run_dir = app.runs_dir / "run-second"
    app.deploy()  # installed now: no second download
    assert calls[0][0].endswith("revive.sh")
    app.cleanup()


def test_a_download_with_the_wrong_sha256_is_removed_and_not_installed(app, monkeypatch):
    calls, command = missing_world(app, b"another package")
    monkeypatch.setattr(world.subprocess, "run", command)
    with pytest.raises(RuntimeError, match="does not match its sha256"):
        app.deploy()
    assert [c[0] for c in calls] == ["curl"]
    assert not (app.runs_dir / "world.tar.zst").exists()
    assert app._lease is None
