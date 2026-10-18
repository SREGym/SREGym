"""Explanations: complete candidate chains that code builds and the decision model chooses between."""

from __future__ import annotations

from dataclasses import dataclass, field

SUPPORTS = "supports"
CONTRADICTS = "contradicts"
UNKNOWN = "unknown"

PARTIAL = "partial"  # "the fault is in X, mechanism unknown"; always available, reported as partial


@dataclass
class Prediction:
    """Something that should be observable if the explanation holds."""

    text: str
    status: str = UNKNOWN  # supports | contradicts | unknown
    group: str | None = None  # facts from one underlying signal share a group and count once
    check: str | None = None  # name of the code check that can decide it, when it is still unknown


@dataclass
class Explanation:
    """One complete chain: what broke, how, the concrete values, and what it visibly affected."""

    key: str  # stable id, e.g. "change:deployment/ns/edge-proxy:env FILTER_RULE"
    pattern: str  # change | acts-on | dependency | mismatch | amplify | bad-input | resource | invalid-state | ...
    origin: str  # object id, "kind/namespace/name" (cluster-scoped objects: "kind/name")
    mechanism: str  # one plain sentence
    values: list[str] = field(default_factory=list)  # concrete mutated details known
    contributing: list[str] = field(default_factory=list)
    impact: list[str] = field(default_factory=list)  # components with observed symptoms linked to the origin
    evidence: list[str] = field(default_factory=list)  # fact ids
    predictions: list[Prediction] = field(default_factory=list)

    def supported_groups(self) -> int:
        """Distinct evidence groups that support this explanation."""
        return len({p.group or p.text for p in self.predictions if p.status == SUPPORTS})

    def contradicted(self) -> bool:
        return any(p.status == CONTRADICTS for p in self.predictions)

    def option_text(self) -> str:
        """How the chain is written out as one option of the final pick-one question."""
        parts = [f"Origin: {self.origin}. {self.mechanism}"]
        if self.values:
            parts.append("Details: " + "; ".join(self.values))
        if self.contributing:
            parts.append("Together with: " + ", ".join(self.contributing))
        if self.impact:
            parts.append("Observed impact on: " + ", ".join(self.impact))
        checked = [f"{p.text} ({p.status})" for p in self.predictions if p.status != UNKNOWN]
        if checked:
            parts.append("Checks: " + "; ".join(checked))
        return " | ".join(parts)
