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

    async def ensure_started(self, reg: AgentRegistration) -> Optional[AgentProcess]:
        if not reg or not reg.kickoff_command:
            return None
        existing = self._procs.get(reg.name)
        if existing and existing.proc.returncode is None:
            return existing

        env = os.environ.copy()
        if reg.kickoff_env:
            env.update(reg.kickoff_env)

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
