import logging
import os
import subprocess
import sys
import threading
from datetime import UTC, datetime
from pathlib import Path

from sregym.service.container_runner import ContainerConfig, ContainerRunner, ExecInput

from .agent_registry import AgentRegistration

logger = logging.getLogger(__name__)


class AgentProcess:
    def __init__(self, name: str, proc: subprocess.Popen):
        self.name = name
        self.proc = proc
        self.started_at = datetime.now(UTC)
        self.container_name: str | None = None  # set when running in container mode


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

        if self._use_containers:
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
        )
        ap = AgentProcess(reg.name, proc)
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
        # No separate workspace mount; agents write everything into /logs.
        self._container_runner.config.logs_path = Path(f"./logs/{reg.name}")
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

        if self._use_containers and self._container_runner:
            container_name = getattr(existing, "container_name", None)
            if container_name:
                ContainerRunner.stop_container(container_name, timeout=timeout)
        else:
            try:
                existing.proc.terminate()
                try:
                    existing.proc.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    existing.proc.kill()
                    existing.proc.wait()
            except Exception:
                pass

        if agent_name in self._procs:
            del self._procs[agent_name]
