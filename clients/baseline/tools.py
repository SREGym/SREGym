"""Run bash commands for the baseline agent.

Each command runs in a new shell inside the agent container. A command that runs past its
timeout is killed along with every process it started.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import time
from dataclasses import dataclass


@dataclass
class CommandResult:
    """Output, exit code and run time of one command."""

    stdout: str
    stderr: str
    exit_code: int
    duration_s: float
    timed_out: bool = False


def run_command(command: str, timeout: int, cwd: str | None = None) -> CommandResult:
    """Run one command line through bash; kill the whole process group on timeout."""
    started = time.monotonic()
    with subprocess.Popen(
        ["bash", "-lc", command],
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        text=True,
        errors="replace",
        start_new_session=True,
    ) as proc:
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
            return CommandResult(stdout, stderr, proc.returncode, round(time.monotonic() - started, 3))
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
            stdout, stderr = proc.communicate()
            return CommandResult(
                stdout,
                stderr + f"\n[command timed out after {timeout}s and was killed]",
                124,
                round(time.monotonic() - started, 3),
                timed_out=True,
            )
