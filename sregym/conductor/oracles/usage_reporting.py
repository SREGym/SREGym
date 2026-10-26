"""A mitigation oracle for the frozen-world grader."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from sregym.conductor.oracles.base import Oracle
from sregym.conductor.oracles.failure import FailureClass
from sregym.service.usage_reporting.runtime import write


class UsageMitigationOracle(Oracle):
    # An early submission waits until D, then extraction may take 15 minutes.
    evaluation_timeout_seconds = 3600
    FAILURE_CLASSES = {
        "world_grader_failed": FailureClass.HARNESS_ERROR,
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
            "--no-recurrence",
            "--runtime-limit",
            "870",
        ]
        with (app.run_dir / "grader.log").open("w") as log:
            try:
                subprocess.run(command, cwd=app.project, stdout=log, stderr=subprocess.STDOUT, check=True, timeout=930)
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
