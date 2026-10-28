"""A mitigation oracle for the frozen-world grader."""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import sys
from pathlib import Path

from sregym.conductor.oracles.base import Oracle
from sregym.conductor.oracles.failure import FailureClass
from sregym.service.usage_reporting.runtime import write

# A full frozen-world extraction plus the private client, queue and recurrence
# forks needs about half an hour after an agent has done real recovery work.
GRADER_SECONDS = 1860


def run_grader(command, *, cwd, log, timeout):
    """Run the grader in its own process group so its private world copy never outlives it."""
    process = subprocess.Popen(command, cwd=cwd, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    try:
        returncode = process.wait(timeout=timeout)
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        process.wait()
    if returncode:
        raise subprocess.CalledProcessError(returncode, command)


class UsageMitigationOracle(Oracle):
    # An early submission waits for the deadline (up to 1800 s) and the capture
    # (up to 600 s), then the grader may take up to GRADER_SECONDS.
    evaluation_timeout_seconds = 4500
    FAILURE_CLASSES = {
        "world_grader_failed": FailureClass.HARNESS_ERROR,
        "world_run_aborted": FailureClass.HARNESS_ERROR,
        "world_mitigation_failed": FailureClass.AGENT_ERROR,
    }

    def __init__(self, problem):
        super().__init__(problem)
        self.result = None

    def evaluate(self, solution=None, trace=None, duration=None):
        if self.result is not None:
            return self.result
        app = self.problem.app
        capsule = app.wait_frozen()
        app.retain_workspace(getattr(app, "agent_workspace", None))
        abort = app.run_dir / "abort.json"
        if abort.exists():
            # A run cut short by the harness says nothing about the agent.
            self.result = self.fail("world_run_aborted", abort=json.loads(abort.read_text()))
            return self.result
        grader = app.grader
        output = grader.parent / "results" / ("sregym-" + app.run_dir.name)
        command = [
            sys.executable,
            "-B",
            str(grader),
            str(app.run_dir),
            "--capture",
            str(capsule),
            "--output",
            str(output),
            "--runtime-limit",
            str(GRADER_SECONDS - 60),
        ]
        with (app.run_dir / "grader.log").open("w") as log:
            try:
                run_grader(command, cwd=app.project, log=log, timeout=GRADER_SECONDS)
                grade = json.loads((output / "GRADE.json").read_text())
                if grade.get("schema") != "sregym-grade-v1" or Path(grade["run"]) != app.run_dir:
                    raise ValueError("Grader result schema/run does not match this attempt")
                status = grade["mitigation"]["status"]
                if status not in {"pass", "fail"}:
                    raise ValueError(f"Grader did not return a pass/fail outcome: {status!r}")
            except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
                self.result = self.fail("world_grader_failed", error=str(exc), log=str(app.run_dir / "grader.log"))
                return self.result
        result = {"success": status == "pass", "grade_path": str(output / "GRADE.json")}
        if status == "fail":
            result.update(self.fail("world_mitigation_failed"))
        write(app.run_dir / "mitigation-result.json", result)
        self.result = result
        return result
