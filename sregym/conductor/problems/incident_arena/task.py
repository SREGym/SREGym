"""Read the Incident Arena task contracts vendored under ``tasks/``.

Each directory holds the files SREGym needs from one Harbor task of
abundant-ai/incident-arena (commit fba011e): the incident ticket
(``instruction.md``), task metadata (``task.toml``), the reference repair
(``solve.sh``), the fault/workload overlay (``task.values.yaml``) and the
grading contract (``ground-truth.yaml``). They are never rendered into the
cluster; problems read thresholds, load profiles and root-cause text from them.
"""

from __future__ import annotations

import copy
import functools
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

TASKS_DIR = Path(__file__).resolve().parent / "tasks"

# The Harbor episode-control paragraph that closes every ticket. SREGym agents
# report and hand back through the conductor's submission API instead.
_HARBOR_PARAGRAPH = re.compile(r"\n\s*When you trust the (?:fix|repair).*\Z", re.DOTALL)


@dataclass(frozen=True)
class IncidentArenaTask:
    slug: str
    directory: Path

    @classmethod
    @functools.cache
    def load(cls, slug: str) -> IncidentArenaTask:
        directory = TASKS_DIR / slug
        if not (directory / "ground-truth.yaml").is_file():
            raise FileNotFoundError(f"Incident Arena task contract not found: {directory}")
        return cls(slug=slug, directory=directory)

    # ------------------------------------------------------------------ raw files
    @functools.cached_property
    def ground_truth(self) -> dict[str, Any]:
        return yaml.safe_load((self.directory / "ground-truth.yaml").read_text())

    @functools.cached_property
    def task_values(self) -> dict[str, Any]:
        return yaml.safe_load((self.directory / "task.values.yaml").read_text()) or {}

    @functools.cached_property
    def metadata(self) -> dict[str, Any]:
        with (self.directory / "task.toml").open("rb") as handle:
            return tomllib.load(handle)["metadata"]

    @functools.cached_property
    def instruction(self) -> str:
        return (self.directory / "instruction.md").read_text()

    # ------------------------------------------------------------------ derived views
    @property
    def ticket(self) -> str:
        """The incident report as a user filed it, without Harbor's hand-back paragraph."""
        text = _HARBOR_PARAGRAPH.sub("", self.instruction).strip()
        # Hard-wrapped tickets read better as paragraphs inside a prompt.
        paragraphs = [" ".join(p.split()) for p in re.split(r"\n\s*\n", text) if p.strip()]
        return "\n\n".join(paragraphs)

    @property
    def soak_s(self) -> float:
        return float(self.metadata.get("soak_s", 180.0))

    @property
    def thresholds(self) -> dict[str, Any]:
        return copy.deepcopy(self.ground_truth.get("thresholds") or {})

    @property
    def outcome_check_ids(self) -> list[str]:
        verification = self.ground_truth.get("verification") or {}
        return [c.get("id") for c in (verification.get("outcome") or {}).get("checks", [])]

    @property
    def gates_latency(self) -> bool:
        """Incident Arena grades latency only where an outcome check references it."""
        return any("latency" in (check_id or "") for check_id in self.outcome_check_ids)

    @property
    def answer_key(self) -> list[dict[str, str]]:
        """The (service, component, mechanism) findings the incident is graded on."""
        findings = self.ground_truth.get("ground_truth_set") or [self.ground_truth.get("ground_truth")]
        return [f for f in findings if f]

    def load_profile(self) -> tuple[str, dict[str, Any]]:
        """The load generator profile the task ran: (name, profile body)."""
        loadgen = self.task_values.get("loadgen") or {}
        name = loadgen.get("profile") or self.metadata.get("profile")
        document = yaml.safe_load(loadgen.get("profilesYaml") or "") or {}
        profiles = document.get("profiles") or {}
        if name not in profiles:
            raise ValueError(f"{self.slug}: load profile {name!r} has no inline definition")
        return name, copy.deepcopy(profiles[name])
