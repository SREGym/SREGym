"""A saved, externally managed company application in the normal stage flow."""

import json
import os
from pathlib import Path

from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.oracles.usage_reporting import UsageMitigationOracle
from sregym.conductor.problems.base import Problem
from sregym.service.usage_reporting.world import BUDGET_SECONDS, UsageReportingApplication


class UsageReporting(Problem):
    uses_kubernetes = False
    run_default_workload = False
    cleanup_timeout_seconds = BUDGET_SECONDS + 2700

    def __init__(self, *, world_root=None, state=None, runs_dir=None):
        super().__init__(UsageReportingApplication(world_root, state, runs_dir))
        default = self.app.grader.parent / "diagnosis" / f"{self.app.state}.json"
        key = json.loads(Path(os.environ.get("SREGYM_USAGE_DIAGNOSIS_KEY", default)).read_text())
        if key.get("schema") != "usage-diagnosis-v1" or key.get("saved_state") != self.app.state:
            raise ValueError("Diagnosis key must use usage-diagnosis-v1 and match the selected saved state")
        cause = key["root_cause"]
        if not all(
            isinstance(cause.get(field), str) and cause[field].strip() for field in ("component", "description")
        ):
            raise ValueError("Diagnosis key requires one root cause with a component and description")
        self.root_cause = self.build_structured_root_cause(
            component=cause["component"], namespace=self.namespace, description=cause["description"]
        )
        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)
        self.mitigation_oracle = UsageMitigationOracle(self)

    def inject_fault(self):
        # Faults and ordinary scheduled operations are already in the capsule.
        self.fault_injected = True
        return True

    def preflight(self):
        self.app.preflight()

    def recover_fault(self):
        # Recovering by resetting the world would change the state being graded.
        # app.cleanup tears down only after the deadline capture is complete.
        if self.app.agent_deadline_monotonic is not None:
            # Retain the actual deadline outcome even when the agent exits or
            # times out without submitting mitigation. It remains a missing
            # submission in the conductor's normal completeness accounting.
            self.mitigation_oracle.evaluate()
        self.fault_injected = False
        return True
