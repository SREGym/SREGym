"""Smart Meter: a two-region telemetry company on QEMU hosts, revived from a saved world (SREGym-applications/smart-meter)."""

from __future__ import annotations

import fcntl
import hashlib
import json
import platform
import shutil
import stat
import subprocess
import sys
import time
import uuid
from pathlib import Path

from sregym.paths import TARGET_MICROSERVICES
from sregym.service.apps.base import Application
from sregym.service.container_runner import get_container_host_bind_address

IN_USE = "Company world is already in use; no services were changed: "
RETAINED_FILE_BYTES = 64 * 2**20  # answer files are small; data dumps are not retained
# Company endpoints listed on the agent's tools page: reporting, operations API, source, metrics, logs, catalog.
AGENT_PORTS = (28310, 28320, 28311, 28330, 28390, 28391, 28392)


def write(path, value):
    path = Path(path)
    temporary = path.with_suffix(".publishing")
    temporary.write_text(json.dumps(value, indent=2) + "\n")
    temporary.replace(path)


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


class SmartMeter(Application):
    metadata_file = Path(__file__).with_name("metadata.json")
    app_name = "smart-meter"
    # The world listens on these ports on host loopback and on the address agent containers reach the host on.
    agent_endpoints = tuple(f"http://host.docker.internal:{port}" for port in AGENT_PORTS)

    def __init__(
        self,
        project: Path | None = None,
        state: str | None = None,
        runs_dir: Path | None = None,
        package: dict | None = None,
    ):
        super().__init__(self.metadata_file)
        self.load_app_json()
        self.project = Path(project or TARGET_MICROSERVICES / "smart-meter").resolve()
        self.states_dir = self.project / "world/vm/states"
        self.grader = self.project / "grader/grade.py"
        self.lifecycle = self.project / "world/sregym_lifecycle.py"  # ready, freeze, teardown
        self.state = state  # the saved world to revive; each problem selects its own
        self.package = package  # url, sha256 and size of the state's package, installed on first use
        self.runs_dir = Path(runs_dir or Path.home() / ".local/state/sregym/smart-meter-worlds").resolve()
        self.run_dir = self.runs_dir / ("run-" + uuid.uuid4().hex)
        self.lock = self.project / ".sregym-world.lock"  # held by the SREGym run that owns the world
        self.description = json.loads(self.metadata_file.read_text())["Desc"]
        self.agent_workspace = None  # set by main.py for each attempt
        self._lease = None
        self._frozen = False
        self._cleaned = False

    def preflight(self):
        if platform.system() != "Linux" or platform.machine() not in {"x86_64", "amd64"}:
            raise RuntimeError("The company world requires a Linux x86_64 host with KVM")
        require_kvm()
        for binary in ("qemu-system-x86_64", "qemu-img", "systemd-run", "ssh", "docker", "gzip", "zstd"):
            if shutil.which(binary) is None:
                raise RuntimeError(f"Company-world dependency is missing: {binary}")
        for path in (self.project / "world/revive.sh", self.project / "world/companyctl", self.lifecycle, self.grader):
            if not path.is_file():
                raise RuntimeError(f"Company-world bundle is missing {path}")
        if self.state is None or not (self.package or self._installed()):
            raise RuntimeError(f"Saved world {self.state!r} is not installed and ready under {self.states_dir}")
        # revive.sh replaces this bundle's global world. Refuse before calling
        # it if anyone already owns a world or a company service.
        active = []
        for path in (self.project / "world/vm").glob("*.pid"):
            try:
                pid = int(path.read_text().strip())
                proc = Path("/proc") / str(pid)
                fields = (proc / "stat").read_text().rsplit(")", 1)[1].split()
                if fields[0] != "Z":
                    active.append(path.stem)
            except (OSError, ValueError):
                continue
        if active:
            raise RuntimeError(IN_USE + ", ".join(sorted(active)))

    def _installed(self):
        return (self.states_dir / self.state / "ready.json").is_file()

    def _install(self):
        """Download the saved world's package, check its sha256 and install it, once."""
        if self._installed():
            return
        url = self.package["url"]
        download = self.runs_dir / url.rsplit("/", 1)[1]
        self.logger.info(f"Saved world {self.state!r} is not installed; downloading it once ({self.package['size']})")
        subprocess.run(["curl", "-fL", "--retry", "5", "-C", "-", "-o", str(download), url], check=True)
        with download.open("rb") as file:
            digest = hashlib.file_digest(file, "sha256").hexdigest()
        if digest != self.package["sha256"]:
            download.unlink()
            raise RuntimeError(f"{url} does not match its sha256; the next run downloads it again")
        subprocess.run([sys.executable, str(self.project / "world/bundle.py"), "install", str(download)], check=True)
        download.unlink()

    def _command(self, action, *, run=None, timeout=240):
        return subprocess.run(
            [
                sys.executable,
                str(self.lifecycle),
                action,
                "--project",
                str(self.project),
                "--run",
                str(run or self.run_dir),
            ],
            check=True,
            timeout=timeout,
        )

    def _world_config(self):
        path = self.project / "world/world-config.json"
        return json.loads(path.read_text()) if path.exists() else {}

    def _probe_run(self):
        # A partial revive can fail before assigning world ownership.
        return Path(self._world_config().get("probe_run", "/"))

    def deploy(self):
        self.preflight()
        self._lease = self.lock.open("a+")
        try:
            try:
                fcntl.flock(self._lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError(IN_USE + f"another SREGym run holds {self.lock}") from exc
            self.runs_dir.mkdir(parents=True, exist_ok=True)
            if self.package:
                self._install()
            self.preflight()  # Recheck after taking the per-bundle lock.
            subprocess.run(
                [
                    str(self.project / "world/revive.sh"),
                    self.state,
                    str(self.run_dir),
                    get_container_host_bind_address(),
                ],
                cwd=self.project,
                check=True,
                timeout=180,
            )
            self._command("ready", timeout=60)
            launch = {
                "start_monotonic": time.monotonic(),
                "start_host_epoch": time.time(),
                "saved_state": self.state,
                "runner": "sregym",
            }
            write(self.run_dir / "launch.json", launch)
            # The world's own tools read this marker to know a run is active.
            artifacts = self.project / "world/artifacts" / self._world_config().get("artifact_subdir", "runs")
            write(artifacts / "run-active.json", launch)
        except BaseException:
            self.cleanup()
            raise

    def freeze(self):
        """Pause every guest and capture the end state, once; returns the capture directory."""
        if not self._frozen:
            self._frozen = True
            self._command("freeze", timeout=600)
        return self.run_dir / "end-state"

    def retain_workspace(self, workspace):
        """Retain ordinary answer files; skip links, agent authentication, unreadable files and large dumps."""
        destination = self.run_dir / "solver-work"
        destination.mkdir(exist_ok=True)
        if workspace is None:
            return
        workspace = Path(workspace)
        skipped = {"unreadable": [], "too_large": []}
        for source in workspace.rglob("*"):
            relative = source.relative_to(workspace)
            if source.is_symlink() or any(p.startswith(".") for p in relative.parts):
                continue
            if any(p in {"auth.json", "codex-home", "sessions"} for p in relative.parts):
                continue
            target = destination / relative
            if source.is_file():
                if source.stat().st_size > RETAINED_FILE_BYTES:
                    # Recovery work can leave multi-GB data copies in the workspace;
                    # retaining them would exhaust the host disk the capture needs.
                    skipped["too_large"].append(str(relative))
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                try:
                    shutil.copyfile(source, target)
                except PermissionError:
                    # Agents may copy root-only files (for example host logs) into
                    # their workspace; grading must not depend on reading them.
                    skipped["unreadable"].append(str(relative))
        if any(skipped.values()):
            write(self.run_dir / "solver-work-skipped.json", skipped)

    def cleanup(self):
        if self._cleaned:
            return
        if self._lease is None:
            self._remove_leftover_world()
            return
        try:
            if self._probe_run() == self.run_dir:
                self._command("teardown")
            self._cleaned = True
        finally:
            self._lease.close()
            self._lease = None

    def _remove_leftover_world(self):
        """Tear down a world an earlier SREGym run left behind; a world started elsewhere is left alone."""
        run = self._probe_run()
        if run.parent != self.runs_dir or (run / "teardown.json").exists():
            return
        with self.lock.open("a+") as lease:
            try:
                fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return  # A live SREGym run still owns it.
            self._command("teardown", run=run)
