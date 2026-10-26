import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from sregym.service.usage_reporting import runtime, world


@pytest.fixture
def app(tmp_path, monkeypatch):
    project = tmp_path / "bundle"
    for name in (
        "build/revive.sh",
        "build/companyctl",
        "grading/grade.py",
        "build/vm/states/P2/ready.json",
    ):
        path = project / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}")
    (project / "build/artifacts/runs").mkdir(parents=True)
    monkeypatch.setattr(world, "require_kvm", lambda: None)
    monkeypatch.setattr(world.shutil, "which", lambda _name: "/test/bin")
    return world.UsageReportingApplication(project, runs_dir=tmp_path / "runs")


def test_opening_is_the_frozen_customer_description(app):
    assert app.state == "P2"
    for name in ("OPENING.md", "COMPANY-TOOLS.md"):
        assert (world.ASSETS / name).read_text().strip() in app.description
    assert app.app_name == "usage-reporting"


def test_missing_kvm_fails_before_commands(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "stat", Mock(side_effect=FileNotFoundError))
    command = Mock()
    monkeypatch.setattr(world.subprocess, "run", command)
    with pytest.raises(RuntimeError, match="KVM is required.*readable/writable"):
        world.require_kvm()
    command.assert_not_called()


def test_preflight_refuses_an_existing_world_without_changes(app, monkeypatch):
    (app.project / "build/vm/eu-parent-01.pid").write_text(str(os.getpid()))
    command = Mock()
    monkeypatch.setattr(world.subprocess, "run", command)
    with pytest.raises(RuntimeError, match="already in use.*eu-parent-01"):
        app.deploy()
    command.assert_not_called()
    assert not app.run_dir.exists()


def test_preflight_accepts_a_stale_pid_and_named_alias(app):
    (app.project / "build/vm/old.pid").write_text("999999999")
    app.state = "saved"
    (app.project / "build/vm/states/aliases.json").write_text('{"saved":"P2"}')
    app.preflight()
    assert app.saved_state.name == "P2"


def test_preflight_accepts_configured_bundle_locations(app, tmp_path, monkeypatch):
    states = tmp_path / "external-states"
    (states / "P2").mkdir(parents=True)
    (states / "P2/ready.json").write_text("{}")
    grader = tmp_path / "external-grading/grade.py"
    grader.parent.mkdir()
    grader.touch()
    monkeypatch.setenv("SREGYM_USAGE_STATES_DIR", str(states))
    monkeypatch.setenv("SREGYM_USAGE_GRADER", str(grader))
    configured = world.UsageReportingApplication(app.project, runs_dir=tmp_path / "runs")
    configured.preflight()
    assert configured.saved_state == states / "P2"
    assert configured.grader == grader


def test_saved_state_name_cannot_be_a_path(app):
    with pytest.raises(ValueError, match="must be a name"):
        world.UsageReportingApplication(app.project, state="../other")


def test_cleanup_before_deploy_does_nothing(app, monkeypatch):
    command = Mock()
    monkeypatch.setattr(world.subprocess, "run", command)
    app.cleanup()
    command.assert_not_called()


def test_deploy_charges_startup_to_one_budget_and_copies_only_public_tools(app, monkeypatch):
    calls = []

    def command(argv, **kwargs):
        calls.append(argv)
        if argv[0].endswith("revive.sh"):
            app.run_dir.mkdir(parents=True)
            (app.project / "build/world-config.json").write_text(json.dumps({"probe_run": str(app.run_dir)}))
        return subprocess.CompletedProcess(argv, 0)

    def watcher(argv, **kwargs):
        runtime.write(app.run_dir / "watch-ready.json", {"pid": 1})
        return SimpleNamespace(poll=lambda: None)

    gateway = Mock(server_port=39123)
    monkeypatch.setattr(world.subprocess, "run", command)
    monkeypatch.setattr(world.subprocess, "Popen", watcher)
    monkeypatch.setattr(world, "get_container_host_bind_address", lambda: "127.0.0.1")
    monkeypatch.setattr(world, "EndpointGateway", Mock(return_value=gateway))
    cleanup = app.cleanup
    monkeypatch.setattr(app, "cleanup", Mock())
    app.deploy()
    launch = json.loads((app.run_dir / "launch.json").read_text())
    assert app.agent_deadline_monotonic == launch["start_monotonic"] + 1800
    assert launch["seconds"] == 1800
    assert calls[0] == [str(app.project / "build/revive.sh"), "P2", str(app.run_dir)]
    assert "ready" in calls[1]
    assert {p.name for p in app.endpoint_access.public_dir.iterdir()} == {
        "companyctl",
        "OPENING.md",
        "COMPANY-TOOLS.md",
        "endpoint_access.py",
        "access.json",
    }
    runtime.write(app.run_dir / "native-capture-complete.json", {})
    cleanup()
    cleanup()
    assert sum("teardown" in c for c in calls) == 1
    gateway.close.assert_called_once()


def fake_runtime(tmp_path, monkeypatch):
    run = tmp_path / "run"
    (run / "disks").mkdir(parents=True)
    vm = tmp_path / "vm"
    vm.mkdir()
    (vm / "eu-work-01.pid").write_text("123")
    (vm / "world-tools.pid").write_text("234")
    (vm / "provider.pid").write_text("345")
    (run / "disks/eu-work-01.qcow2").write_bytes(b"disk")
    (tmp_path / "public-instances.json").write_text("{}")
    runtime.write(run / "launch.json", {"start_monotonic": 10, "seconds": 1800})
    config = {
        "probe_run": str(run),
        "fronts": {"eu-work-01": str(run / "disks/eu-work-01.qcow2")},
        "private_client_disk": str(run / "disks/ops-helper-01.qcow2"),
        "disk_subdir": str(run / "disks"),
    }
    paused = []

    def result(name, command):
        if command == "stop":
            paused.append(name)
            return {}
        assert command == "query-status"
        return {"status": "paused"}

    def export(name, path):
        assert paused == ["eu-work-01"]
        assert (run / "native-cutoff.json").exists()
        path.write_bytes(b"native-ram")

    lab = SimpleNamespace(
        VM=vm,
        ROOT=tmp_path,
        RESCUE_MARK="rescue",
        pid_of=lambda p: int(p.read_text()) if p.exists() else None,
        owned=lambda pid, expected: (pid == 123) == (expected == "qemu-system-x86_64"),
        stop_pid=Mock(),
    )
    revive = SimpleNamespace(result=Mock(side_effect=result), export_native=export, stop_owned=Mock())
    guard = SimpleNamespace(freeze_family=Mock(return_value={"frozen": True}), stop_frozen_family=Mock())
    monkeypatch.setattr(runtime, "modules", lambda _p: (lab, revive, SimpleNamespace(config=lambda: config), guard))
    monkeypatch.setattr(runtime, "stop_agent", lambda r: runtime.write(r / "solver-cutoff.json", {}))
    return run, lab, revive, config, guard


def test_freeze_pauses_before_export_and_preserves_native_images(tmp_path, monkeypatch):
    run, lab, revive, config, guard = fake_runtime(tmp_path, monkeypatch)
    runtime.freeze(tmp_path, run)
    capsule = run / "end-state"
    manifest = json.loads((capsule / "manifest.json").read_text())
    assert manifest["cutoff"]["target_monotonic"] == 1810
    assert manifest["live"] == ["eu-work-01"]
    assert Path(manifest["ram"]["eu-work-01"]).read_bytes() == b"native-ram"
    disk = Path(manifest["disks"]["eu-work-01"])
    assert disk.read_bytes() == b"disk"
    assert disk.stat().st_mode & 0o222 == 0
    assert (run / "native-capture-complete.json").exists()
    assert not any(call.args[1] == "cont" for call in revive.result.call_args_list)
    assert guard.freeze_family.call_count == 2
    runtime.teardown(tmp_path, run)
    revive.stop_owned.assert_called_once()
    assert guard.stop_frozen_family.call_count == 2


def test_freeze_error_never_certifies_a_complete_capture(tmp_path, monkeypatch):
    run, _lab, revive, _config, _guard = fake_runtime(tmp_path, monkeypatch)
    revive.result.side_effect = RuntimeError("QMP unavailable")
    with pytest.raises(RuntimeError, match="Incomplete deadline freeze"):
        runtime.freeze(tmp_path, run)
    assert (run / "native-cutoff.json").exists()
    assert not (run / "native-capture-complete.json").exists()


def test_teardown_refuses_changed_ownership(tmp_path, monkeypatch):
    run, lab, revive, config, _guard = fake_runtime(tmp_path, monkeypatch)
    config["probe_run"] = str(tmp_path / "someone-else")
    with pytest.raises(RuntimeError, match="ownership changed"):
        runtime.teardown(tmp_path, run)
    revive.stop_owned.assert_not_called()
    lab.stop_pid.assert_not_called()


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


def test_capture_failure_still_tears_down_owned_processes(app, monkeypatch):
    app.run_dir.mkdir(parents=True)
    (app.project / "build/world-config.json").write_text(json.dumps({"probe_run": str(app.run_dir)}))
    app._owned = True
    app.agent_deadline_monotonic = 10
    command = Mock()
    monkeypatch.setattr(app, "_command", command)
    monkeypatch.setattr(app, "wait_frozen", Mock(side_effect=RuntimeError("Incomplete capture")))
    with pytest.raises(RuntimeError, match="Incomplete capture"):
        app.cleanup()
    command.assert_called_once_with("teardown")
    app.cleanup()
    command.assert_called_once()


def test_resource_guard_must_report_a_fresh_sample(tmp_path, monkeypatch):
    artifacts = tmp_path / "artifacts/runs"
    artifacts.mkdir(parents=True)
    run = tmp_path / "run"
    run.mkdir()
    sample = {"epoch": 100, "stop_reasons": [], "CPU_PSI_and_load_are_telemetry_only": True}
    (artifacts / "resource-current.json").write_text(json.dumps(sample))
    lab = SimpleNamespace(
        ROOT=tmp_path,
        VM=tmp_path / "vm",
        ARTIFACTS=tmp_path / "artifacts",
        start_background=Mock(),
        pid_of=lambda _p: 123,
        owned=lambda *_a: True,
    )
    monkeypatch.setattr(runtime.time, "time", lambda: 100)
    runtime.start_resource_guard(lab, run)
    assert json.loads((run / "resource-guard.json").read_text())["pid"] == 123
    assert runtime.resource_problem(lab) is None
    sample["stop_reasons"] = ["swap"]
    (artifacts / "resource-current.json").write_text(json.dumps(sample))
    assert runtime.resource_problem(lab) == "Host resource limit: swap"
    with pytest.raises(RuntimeError, match="preflight failed: swap"):
        runtime.start_resource_guard(lab, run)
