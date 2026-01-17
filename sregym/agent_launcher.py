import os
import subprocess
import sys
import threading
from datetime import datetime
from typing import Dict, Optional

from .agent_registry import AgentRegistration


class AgentProcess:
    def __init__(self, name: str, proc: subprocess.Popen):
        self.name = name
        self.proc = proc
        self.started_at = datetime.utcnow()


class AgentLauncher:
    def __init__(self):
        self._procs: Dict[str, AgentProcess] = {}
        self._agent_kubeconfig_path: Optional[str] = None

    def set_agent_kubeconfig(self, kubeconfig_path: Optional[str]):
        """
        Set the kubeconfig path that agents should use.
        This is typically the filtered kubeconfig from the K8s proxy.
        """
        self._agent_kubeconfig_path = kubeconfig_path

    async def ensure_started(self, reg: AgentRegistration) -> Optional[AgentProcess]:
        if not reg or not reg.kickoff_command:
            return None
        existing = self._procs.get(reg.name)

        if existing:
            existing.proc.poll()
            if existing.proc.returncode is None:
                return existing

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
            # Already terminated, just remove from cache
            del self._procs[agent_name]
            return

        # Try graceful termination
        try:
            existing.proc.terminate()
            try:
                existing.proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                # Force kill if timeout exceeded
                existing.proc.kill()
                existing.proc.wait()
        except Exception:
            pass
        finally:
            # Remove from cache
            if agent_name in self._procs:
                del self._procs[agent_name]
