"""A saved, externally managed company application in the normal stage flow."""

import json
from pathlib import Path

from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.oracles.usage_reporting import UsageMitigationOracle
from sregym.conductor.problems.base import Problem
from sregym.service.apps.smart_meter.application import SmartMeter
from sregym.utils.decorators import mark_fault_injected

ASSETS = Path(__file__).resolve().parent / "assets/usage_reporting"
# Answer keys stay in SREGym, outside the application; only OPENING.md and COMPANY-TOOLS.md reach the agent.
KEYS = ASSETS / "diagnosis"
# The saved world, one package; the first run downloads and installs it.
PACKAGE = {
    "url": "https://huggingface.co/datasets/munimthahmid/usage-reporting-world/resolve/main/smart-meter-usage-reporting.tar.zst",
    "sha256": "8817fc4aba6995c18d27b6099df92f00f8d0e74d688015505765595573cf51cd",
    "size": "10.2 GB",
}


class UsageReporting(Problem):
    uses_cluster = False
    run_default_workload = False
    agent_endpoints = SmartMeter.agent_endpoints
    # A company command may stay silent for up to 1,800 s, past mitmproxy's 600 s default.
    egress_idle_timeout = 3600

    def __init__(self, *, world_root=None, state=None, runs_dir=None):
        super().__init__(
            SmartMeter(
                world_root,
                state=state or "usage-reporting",
                runs_dir=runs_dir or Path.home() / ".local/state/sregym/usage-worlds",
                package=PACKAGE,
            )
        )
        self.app.description = "\n\n".join(
            (ASSETS / name).read_text().strip() for name in ("OPENING.md", "COMPANY-TOOLS.md")
        )
        key = json.loads((KEYS / f"{self.app.state}.json").read_text())
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

    @mark_fault_injected
    def inject_fault(self):
        """Faults and ordinary scheduled operations are already in the saved world."""

    @mark_fault_injected
    def recover_fault(self):
        """Resetting the world would change the state being graded; app.cleanup tears it down."""
