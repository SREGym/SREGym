"""Versioned scenario specs for OpenStack incident families.

A scenario file ties one public postmortem to an executable environment: where
the facts come from, how each element of the original maps onto the region, the
known deviations, the fault's per-tier parameters and the grading thresholds.
Problems read every tunable from it, so a tier or threshold change is a reviewed
data change, and the doc for an incident family is its spec.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from sregym.service.apps.openstack.ledger import SLO
from sregym.service.apps.openstack.tiers import TIERS

SCENARIOS = Path(__file__).with_name("scenarios")
SUPPORTED_VERSIONS = {1}
REQUIRED = ("version", "id", "title", "postmortem", "environment", "fault", "tiers", "grading", "diagnosis")
POSTMORTEM_REQUIRED = ("provider", "incident", "sources", "chain")
CHAIN_REQUIRED = ("trigger", "mechanism", "amplifier", "impact")


class ScenarioError(ValueError):
    pass


@dataclass(frozen=True)
class Scenario:
    path: Path
    data: dict

    @property
    def id(self) -> str:
        return self.data["id"]

    @property
    def tiers(self) -> tuple[str, ...]:
        return tuple(self.data["tiers"])

    def fault(self, tier: str) -> dict:
        if tier not in self.data["tiers"]:
            raise ScenarioError(f"{self.id} does not define tier {tier!r}; choose from {self.tiers}")
        return {**self.data["fault"].get("params", {}), **(self.data["tiers"][tier] or {})}

    @property
    def grading(self) -> dict:
        return self.data["grading"]

    @property
    def invariants(self) -> tuple[str, ...]:
        return tuple(self.data["grading"].get("invariants", ()))

    def slo(self) -> SLO:
        spec = self.data["grading"]["slo"]
        return SLO(
            min_success=dict(spec["min_success"]),
            min_attempts=dict(spec.get("min_attempts", {})),
            tenant_min_success=dict(spec.get("tenant_min_success", {})),
        )

    def diagnosis(self, **facts) -> dict:
        spec = self.data["diagnosis"]
        return {
            "component": spec["component"].format(**facts),
            "description": " ".join(spec["description"].split()).format(**facts),
        }


def validate(data: dict, *, known_invariants: set[str] | None = None) -> list[str]:
    problems = [f"missing key {key!r}" for key in REQUIRED if key not in data]
    if problems:
        return problems
    if data["version"] not in SUPPORTED_VERSIONS:
        problems.append(f"unsupported version {data['version']!r}")
    postmortem = data["postmortem"]
    problems += [f"postmortem missing {key!r}" for key in POSTMORTEM_REQUIRED if key not in postmortem]
    problems += [
        f"postmortem.chain missing {key!r}" for key in CHAIN_REQUIRED if key not in postmortem.get("chain", {})
    ]
    if not all(str(url).startswith("https://") for url in postmortem.get("sources", ())):
        problems.append("postmortem.sources must be https URLs")
    unknown = sorted(set(data["tiers"]) - set(TIERS))
    if unknown:
        problems.append(f"unknown tiers {unknown}; the region defines {sorted(TIERS)}")
    if not data["environment"].get("mapping"):
        problems.append("environment.mapping must map postmortem elements to the environment")
    if "deviations" not in data["environment"]:
        problems.append("environment.deviations must be stated, even if empty")
    slo = data["grading"].get("slo", {})
    if not slo.get("min_success"):
        problems.append("grading.slo.min_success is required")
    if known_invariants is not None:
        extra = sorted(set(data["grading"].get("invariants", ())) - known_invariants)
        if extra:
            problems.append(f"unknown invariants {extra}")
    return problems


def load(name: str, *, known_invariants: set[str] | None = None) -> Scenario:
    path = SCENARIOS / f"{name}.yaml"
    data = yaml.safe_load(path.read_text())
    problems = validate(data, known_invariants=known_invariants)
    if problems:
        raise ScenarioError(f"Invalid scenario {path}: {'; '.join(problems)}")
    return Scenario(path, data)
