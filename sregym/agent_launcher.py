import contextlib
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

from sregym.service.container_runner import ContainerConfig, ContainerRunner, ExecInput

from .agent_registry import AgentRegistration

logger = logging.getLogger(__name__)


class AgentProcess:
    def __init__(self, name: str, proc: subprocess.Popen, pgid: int | None = None):
        self.name = name
        self.proc = proc
        self.started_at = datetime.now(UTC)
        self.container_name: str | None = None  # set when running in container mode
        # Process-group id for shell-launched (non-container) agents. Set so that
        # cleanup can signal the whole process tree, not just the shell wrapper.
        self.pgid = pgid


class AgentLauncher:
    def __init__(self):
        self._procs: dict[str, AgentProcess] = {}
        self._agent_kubeconfig_path: str | None = None
        self._use_containers: bool = True
        self._container_runner: ContainerRunner | None = None

    def set_agent_kubeconfig(self, kubeconfig_path: str | None):
        """
        Set the kubeconfig path that agents should use.
        This is typically the filtered kubeconfig from the K8s proxy.
        """
        self._agent_kubeconfig_path = kubeconfig_path

    def enable_container_isolation(self, force_build: bool = False):
        """Initialize the container runner and build/check the image."""
        if not self._container_runner:
            config = ContainerConfig(
                kubeconfig_path=Path(self._agent_kubeconfig_path) if self._agent_kubeconfig_path else None,
                logs_path=Path("./logs"),
                sregym_apps_path=Path("./SREGym-applications"),
                sregym_app_subdirs=["socialNetwork/wrk2", "hotelReservation/wrk2"],
            )
            self._container_runner = ContainerRunner(config)
            if force_build:
                self._container_runner.build_image()
            else:
                self._container_runner.ensure_image_exists()

    async def ensure_started(self, reg: AgentRegistration) -> AgentProcess | None:
        if not reg or not reg.kickoff_command:
            return None
        existing = self._procs.get(reg.name)

        if existing:
            existing.proc.poll()
            if existing.proc.returncode is None:
                return existing

        if self._use_containers and reg.container_isolation:
            return await self._start_containerized(reg)

        env = os.environ.copy()
        if reg.kickoff_env:
            env.update(reg.kickoff_env)

        # Use filtered kubeconfig if set (hides chaos engineering namespaces)
        if self._agent_kubeconfig_path:
            env["KUBECONFIG"] = self._agent_kubeconfig_path

        proc = subprocess.Popen(
            reg.kickoff_command,
            shell=True,
            cwd=reg.kickoff_workdir or os.getcwd(),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            universal_newlines=True,
            # Run the shell wrapper in its own session so it becomes the leader
            # of a new process group (pgid == proc.pid). This lets cleanup signal
            # the entire agent process tree instead of only the shell wrapper,
            # which otherwise leaves orphaned driver/agent children running.
            start_new_session=True,
        )
        ap = AgentProcess(reg.name, proc, pgid=proc.pid)
        self._procs[reg.name] = ap
        t = threading.Thread(target=self._pipe_logs, args=(reg.name, proc), daemon=True)
        t.start()
        return ap

    def _pipe_logs(self, name: str, proc: subprocess.Popen):
        if proc.stdout is None:
            return
        for line in proc.stdout:
            try:
                sys.stdout.write(f"{line}")
                sys.stdout.flush()
            except Exception:
                break

    async def _start_containerized(self, reg: AgentRegistration) -> AgentProcess | None:
        """Start an agent in a Docker container with install-then-run pattern."""
        if not reg.kickoff_command:
            logger.warning("No kickoff command defined for agent '%s' — skipping containerized start", reg.name)
            return None

        if not self._container_runner:
            logger.warning("Container runner not initialized — skipping containerized start for '%s'", reg.name)
            return None

        if self._agent_kubeconfig_path:
            self._container_runner.config.kubeconfig_path = Path(self._agent_kubeconfig_path)

        # Set per-agent logs path — also used as the container working directory.
        # If AGENT_LOGS_DIR is set by the orchestrator (e.g. run_1/), mount that
        # host directory to /logs so the agent writes into the right run folder.
        # Otherwise fall back to the default per-agent logs directory.
        agent_logs_dir = os.environ.get("AGENT_LOGS_DIR")
        self._container_runner.config.logs_path = Path(agent_logs_dir) if agent_logs_dir else Path(f"./logs/{reg.name}")
        self._container_runner.config.workspace_path = None

        composite_cmd = self._container_runner.build_composite_command(
            install_script=reg.install_script,
            agent_version=reg.agent_version,
            driver_command=reg.kickoff_command,
        )

        exec_input = ExecInput(
            command=composite_cmd,
            env=reg.kickoff_env or {},
            label=f"{reg.name}-run",
        )
        exec_input.env.setdefault("AGENT_LOGS_DIR", "/logs")

        proc = self._container_runner.run_async(exec_input)
        ap = AgentProcess(reg.name, proc)
        ap.container_name = exec_input.container_name  # track for cleanup
        self._procs[reg.name] = ap
        t = threading.Thread(target=self._pipe_logs, args=(reg.name, proc), daemon=True)
        t.start()
        return ap

    def cleanup_all(self, timeout: int = 10) -> None:
        """Terminate and cleanup all tracked agent processes/containers."""
        for name in list(self._procs):
            self.cleanup_agent(name, timeout=timeout)

    def cleanup_agent(self, agent_name: str, timeout: int = 5) -> None:
        """
        Terminate and cleanup an agent process.

        Args:
            agent_name: Name of the agent to cleanup
            timeout: Seconds to wait for graceful termination before killing
        """
        existing = self._procs.get(agent_name)
        if not existing:
            return

        # Check if already terminated
        existing.proc.poll()
        if existing.proc.returncode is not None:
            del self._procs[agent_name]
            return

        # Choose the teardown path based on how THIS process was launched
        # (tracked per-process via container_name), not the launcher's global
        # container-runner state. Otherwise a shell-launched agent could fall
        # into the container branch, find no container_name, and never be killed.
        container_name = getattr(existing, "container_name", None)
        if container_name:
            ContainerRunner.stop_container(container_name, timeout=timeout)
        else:
            self._terminate_process_group(existing, timeout)

        if agent_name in self._procs:
            del self._procs[agent_name]

    def _terminate_process_group(self, ap: AgentProcess, timeout: int) -> None:
        """Terminate a shell-launched agent and its entire process group.

        Signals the whole process group (the shell wrapper plus the driver and
        any children it spawned) so no orphaned processes survive. Falls back to
        single-process termination if no process-group id was captured.
        """
        proc = ap.proc

        if ap.pgid is None:
            self._terminate_single(proc, timeout)
            return

        try:
            os.killpg(ap.pgid, signal.SIGTERM)
        except ProcessLookupError:
            # Group already gone; still reap the wrapper to avoid a zombie.
            with contextlib.suppress(Exception):
                proc.wait(timeout=timeout)
            return

        # Wait for the *whole group* to drain, not just the shell wrapper. The
        # wrapper often exits immediately on SIGTERM while the real driver (or a
        # SIGTERM-ignoring child) keeps running, so polling proc alone would skip
        # the SIGKILL escalation and leave orphans behind.
        if not self._wait_for_group_exit(ap.pgid, proc, timeout):
            with contextlib.suppress(ProcessLookupError):
                os.killpg(ap.pgid, signal.SIGKILL)
            self._wait_for_group_exit(ap.pgid, proc, timeout)

        # Reap the wrapper so it does not linger as a zombie.
        with contextlib.suppress(Exception):
            proc.wait(timeout=timeout)

    @staticmethod
    def _wait_for_group_exit(pgid: int, proc: subprocess.Popen, timeout: int, interval: float = 0.05) -> bool:
        """Poll until no process remains in the group, or timeout. Returns True if drained.

        ``proc`` (the group leader / shell wrapper) is reaped opportunistically via
        ``poll()`` each iteration. Otherwise an unreaped zombie leader keeps the
        group "alive" for ``killpg(pgid, 0)`` and would mask that all real children
        have already exited.
        """
        deadline = time.monotonic() + timeout
        while True:
            proc.poll()  # reap the leader if it has exited, so it stops counting
            try:
                os.killpg(pgid, 0)  # signal 0 == existence check for the group
            except ProcessLookupError:
                return True  # no live members left
            if time.monotonic() >= deadline:
                return False
            time.sleep(interval)

    def _terminate_single(self, proc: subprocess.Popen, timeout: int) -> None:
        """Terminate a single process (fallback when no pgid is available)."""
        try:
            proc.terminate()
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        except Exception:
            pass
