"""Application adapter for a prebuilt company-world bundle."""

from __future__ import annotations

import fcntl
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import time
import uuid
from pathlib import Path

from sregym.service.apps.base import Application
from sregym.service.container_runner import get_container_host_bind_address
from sregym.service.endpoint_access import EndpointAccess, EndpointGateway
from sregym.service.usage_reporting.runtime import write

PORTS = (28310, 28320, 28311, 28330, 28390, 28391, 28392)
ASSETS = Path(__file__).resolve().parents[2] / "conductor/problems/assets/usage_reporting"
RUNTIME = Path(__file__).with_name("runtime.py")
BUDGET_SECONDS = 1800


def require_kvm():
    try:
        if not stat.S_ISCHR(Path("/dev/kvm").stat().st_mode):
            raise OSError("not a character device")
        with open("/dev/kvm", "rb+"):
            pass
    except OSError as exc:
        raise RuntimeError(
            "KVM is required: /dev/kvm must exist and be readable/writable by the conductor user"
        ) from exc


class UsageReportingApplication(Application):
    namespace = "company"
    app_name = name = "usage-reporting"
    operator_instructions = (
        "Use companyctl and the company endpoints listed above. Guest shells contain the installed tools. "
        "Submit your diagnosis through the conductor, then complete and submit mitigation. "
        "The single 1800-second budget includes agent startup, diagnosis judging and mitigation. "
        "The service continues until the deadline even if you submit early. "
        "Put findings and supporting files in your workspace before the deadline."
    )

    def __init__(self, project: Path | None = None, state: str | None = None, runs_dir: Path | None = None):
        root = project or os.environ.get("SREGYM_USAGE_WORLD_ROOT")
        if not root:
            raise RuntimeError("Set SREGYM_USAGE_WORLD_ROOT to the installed company-world bundle directory")
        self.project = Path(root).resolve()
        self.states_dir = Path(os.environ.get("SREGYM_USAGE_STATES_DIR", self.project / "build/vm/states")).resolve()
        self.grader = Path(os.environ.get("SREGYM_USAGE_GRADER", self.project / "grading/grade.py")).resolve()
        self.state = state or os.environ.get("SREGYM_USAGE_STATE", "P2")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", self.state):
            raise ValueError("Saved-world state must be a name, for example P2")
        default_runs = Path.home() / ".local/state/sregym/usage-worlds"
        self.runs_dir = Path(runs_dir or os.environ.get("SREGYM_USAGE_RUNS_DIR", default_runs)).resolve()
        self.run_dir = self.runs_dir / ("run-" + uuid.uuid4().hex)
        self.description = "\n\n".join(
            (ASSETS / name).read_text().strip() for name in ("OPENING.md", "COMPANY-TOOLS.md")
        )
        self.agent_deadline_monotonic = None
        self.endpoint_access = None
        self.gateway = None
        self.watcher = None
        self._lease = None
        self._owned = False
        self._cleaned = False

    def preflight(self):
        if platform.system() != "Linux" or platform.machine() not in {"x86_64", "amd64"}:
            raise RuntimeError("The company world requires a Linux x86_64 host with KVM")
        require_kvm()
        for binary in ("qemu-system-x86_64", "qemu-img", "systemd-run", "ssh", "docker", "gzip"):
            if shutil.which(binary) is None:
                raise RuntimeError(f"Company-world dependency is missing: {binary}")
        for path in (self.project / "build/revive.sh", self.project / "build/companyctl", self.grader):
            if not path.is_file():
                raise RuntimeError(f"Company-world bundle is missing {path}")
        states = self.states_dir
        aliases = json.loads((states / "aliases.json").read_text()) if (states / "aliases.json").exists() else {}
        selected = (states / aliases.get(self.state, self.state)).resolve()
        if not selected.is_relative_to(states.resolve()) or not (selected / "ready.json").is_file():
            raise RuntimeError(f"Saved world {self.state!r} is not installed and ready under {states}")
        self.saved_state = selected
        # revive.sh replaces this bundle's global world. Refuse before calling
        # it if anyone already owns a world or a company service.
        active = []
        for path in (self.project / "build/vm").glob("*.pid"):
            try:
                pid = int(path.read_text().strip())
                proc = Path("/proc") / str(pid)
                fields = (proc / "stat").read_text().rsplit(")", 1)[1].split()
                if fields[0] != "Z":
                    active.append(path.stem)
            except (OSError, ValueError):
                continue
        if active:
            raise RuntimeError(
                "Company world is already in use; no services were changed: " + ", ".join(sorted(active))
            )

    def _command(self, action, *, timeout=240):
        return subprocess.run(
            [sys.executable, str(RUNTIME), action, "--project", str(self.project), "--run", str(self.run_dir)],
            check=True,
            timeout=timeout,
        )

    def deploy(self):
        self.preflight()
        self._lease = (self.project / ".sregym-usage.lock").open("a+")
        try:
            fcntl.flock(self._lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.preflight()  # Recheck after taking the per-bundle lease.
            self.runs_dir.mkdir(parents=True, exist_ok=True)
            self._owned = True
            subprocess.run(
                [str(self.project / "build/revive.sh"), self.state, str(self.run_dir)],
                cwd=self.project,
                check=True,
                timeout=180,
            )
            self._command("ready", timeout=60)
            start = time.monotonic()
            self.agent_deadline_monotonic = start + BUDGET_SECONDS
            launch = {
                "start_monotonic": start,
                "start_host_epoch": time.time(),
                "seconds": BUDGET_SECONDS,
                "saved_state": self.state,
                "runner": "sregym",
                "controller_pid": os.getpid(),
            }
            write(self.run_dir / "launch.json", launch)
            config = json.loads((self.project / "build/world-config.json").read_text())
            artifacts = self.project / "build/artifacts" / config.get("artifact_subdir", "runs")
            write(artifacts / "run-active.json", launch)
            with (self.run_dir / "world-cutoff.log").open("w") as log:
                self.watcher = subprocess.Popen(
                    [
                        sys.executable,
                        str(RUNTIME),
                        "watch",
                        "--project",
                        str(self.project),
                        "--run",
                        str(self.run_dir),
                        "--parent-pid",
                        str(os.getpid()),
                    ],
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            self._wait_file("watch-ready.json", timeout=10)
            api_port = int(os.environ.get("API_PORT", "8000"))
            if api_port in PORTS:
                raise ValueError("API_PORT must differ from the company endpoint ports")
            self.gateway = EndpointGateway(
                get_container_host_bind_address(),
                PORTS,
                api_port,
                is_open=lambda: time.monotonic() < self.agent_deadline_monotonic,
            )
            self.gateway.start()
            public = self.run_dir / "public-tools"
            public.mkdir()
            for name in ("OPENING.md", "COMPANY-TOOLS.md"):
                shutil.copyfile(ASSETS / name, public / name)
            shutil.copyfile(self.project / "build/companyctl", public / "companyctl")
            (public / "companyctl").chmod(0o755)
            import sregym.service.endpoint_access as access_module

            shutil.copyfile(access_module.__file__, public / "endpoint_access.py")
            gateway_url = f"http://host.docker.internal:{self.gateway.server_port}"
            write(
                public / "access.json",
                {
                    "ports": [*PORTS, api_port],
                    "conductor_port": api_port,
                    "gateway_url": gateway_url,
                    "deadline_monotonic": self.agent_deadline_monotonic,
                },
            )
            self.endpoint_access = EndpointAccess(
                public, gateway_url, self.run_dir / "agent-container.json", self.agent_deadline_monotonic
            )
        except BaseException:
            if self.agent_deadline_monotonic is not None:
                write(self.run_dir / "abort.json", {"reason": "Deployment did not finish"})
                if self.watcher is None:
                    self._command("freeze", timeout=600)
            self.cleanup()
            if self._lease is not None:
                self._lease.close()
                self._lease = None
            raise

    def _wait_file(self, name, timeout):
        end = time.monotonic() + timeout
        while not (self.run_dir / name).exists():
            error = self.run_dir / "capture-error.json"
            if error.exists():
                raise RuntimeError(error.read_text())
            if self.watcher is not None and self.watcher.poll() is not None:
                raise RuntimeError(f"World cutoff helper exited; see {self.run_dir / 'world-cutoff.log'}")
            if time.monotonic() >= end:
                raise TimeoutError(f"Timed out waiting for world {name}")
            time.sleep(0.05)
        return self.run_dir / name

    def wait_frozen(self):
        if self.agent_deadline_monotonic is None:
            raise RuntimeError("Company world has not been deployed")
        timeout = max(0, self.agent_deadline_monotonic - time.monotonic()) + 600
        self._wait_file("native-capture-complete.json", timeout)
        return self.run_dir / "end-state"

    def retain_workspace(self, workspace):
        """Retain ordinary answer files; ignore links and agent authentication."""
        destination = self.run_dir / "solver-work"
        destination.mkdir(exist_ok=True)
        if workspace is None:
            return
        workspace = Path(workspace)
        for source in workspace.rglob("*"):
            relative = source.relative_to(workspace)
            if source.is_symlink() or any(p.startswith(".") for p in relative.parts):
                continue
            if any(p in {"auth.json", "codex-home", "sessions"} for p in relative.parts):
                continue
            target = destination / relative
            if source.is_file():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)

    def cleanup(self):
        if not self._owned or self._cleaned:
            return
        capture_error = None
        try:
            if self.agent_deadline_monotonic is not None:
                try:
                    self.wait_frozen()
                except (RuntimeError, TimeoutError) as exc:
                    capture_error = exc
            # Partial revive can fail before assigning world ownership.
            config_path = self.project / "build/world-config.json"
            config = json.loads(config_path.read_text()) if config_path.exists() else {}
            if Path(config.get("probe_run", "/")) == self.run_dir:
                self._command("teardown")
            self._cleaned = True
        finally:
            if self.gateway is not None:
                self.gateway.close()
                self.gateway = None
            if self._lease is not None:
                self._lease.close()
                self._lease = None
        if capture_error is not None:
            raise capture_error
