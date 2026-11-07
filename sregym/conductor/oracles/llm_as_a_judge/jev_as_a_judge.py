"""Jev-as-a-Judge: the RCA checklist asked to the Jev decision model, alone or before the LLM judge.

Jev answers the 9 questions of ``rca_checklists.yaml`` as P(yes) in one request, each rewritten against the fault-spec
fields of the expectation (component, namespace, description). The score is the checklist-weighted mean of the 9 P(yes)
values. The thresholds are frozen in ``jev_as_a_judge.yaml``:

- ``jev``: pass iff score >= ``cut_jev_alone``.
- ``mixed``: reports Jev is unsure about (score in [``band_low``, ``band_high``]) go to the LLM judge, whose verdict is
  final; otherwise pass iff score > ``band_high``.

The question texts must stay as they are: the thresholds were chosen for exactly these requests.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

JEV_AS_A_JUDGE_CONFIG = Path(__file__).parent / "jev_as_a_judge.yaml"
CHECKLIST = Path(__file__).parent / "rca_checklists.yaml"
MODES = ("llm", "jev", "mixed")

RULES = (
    "`ground_truth` is the authoritative reference: `component` is the fault origin, `namespace` its namespace, "
    "and `description` explains the injected fault (it may also mention a mitigation, which is not part of "
    "the question). Answer based only on what `diagnosis` states explicitly."
)

# (instructions, true, false) per checklist question
QUESTIONS = {
    "D1-Q1": (
        "Does `diagnosis` name the object in `ground_truth.component` as the root cause (the same object; a "
        "different kind prefix or its pods count as the same)?",
        "Yes: the root cause the diagnosis names is ground_truth.component.",
        "No: the diagnosis names another object as the root cause, names none, or lists several candidates "
        "without choosing one.",
    ),
    "D1-Q2": (
        "Does `diagnosis` treat `ground_truth.component` as the origin and the other affected components as "
        "downstream or secondary, in the same way `ground_truth.description` does?",
        "Yes: the origin and the secondary failures are told apart as in the ground truth.",
        "No: the diagnosis presents a secondary or downstream component as the origin, or does not tell them apart.",
    ),
    "D1-Q3": (
        "Does `diagnosis` avoid presenting as the fault origin a component that the ground truth does not name "
        "as the origin (for example one it calls healthy, innocent or a victim)?",
        "Yes: no such component is presented as the origin.",
        "No: the diagnosis presents a component other than ground_truth.component as the origin.",
    ),
    "D2-Q1": (
        "Does `diagnosis` describe the same fault mechanism as `ground_truth.description`: what was changed or "
        "broken on the origin (for example a wrong port, a missing env var, a bad image, a wrong selector, a "
        "resource limit, a policy, a rule, credentials)?",
        "Yes: the diagnosis describes the same kind of change or breakage on the origin.",
        "No: it describes a different mechanism, or no mechanism.",
    ),
    "D2-Q2": (
        "Does `diagnosis` state a concrete detail of the injected change that matches "
        "`ground_truth.description`: the field, key, value, port, selector, image tag, limit, rule or name that "
        "was altered?",
        "Yes: at least one concrete detail matches the ground truth and none contradicts it.",
        "No: the details are missing or generic, or they contradict the ground truth.",
    ),
    "D2-Q3": (
        "Does `diagnosis` avoid attributing the fault to a fault type that conflicts with `ground_truth.description`?",
        "Yes: the fault type the diagnosis gives is consistent with the ground truth.",
        "No: the diagnosis gives a conflicting fault type (for example overload instead of a "
        "misconfiguration, or a code bug instead of a policy).",
    ),
    "D3-Q1": (
        "Does `diagnosis` avoid blaming components that `ground_truth.description` does not describe as "
        "contributing to the fault?",
        "Yes: every component the diagnosis blames as a cause or co-cause contributes to the fault in the "
        "ground truth.",
        "No: the diagnosis also blames components that the ground truth does not describe as contributing.",
    ),
    "D3-Q2": (
        "Does `diagnosis` mention every component that `ground_truth.description` describes as contributing to "
        "or affected by the fault?",
        "Yes: all of them are mentioned.",
        "No: at least one of them is missing.",
    ),
    "D3-Q3": (
        "Does `diagnosis` describe an impact or symptom consistent with `ground_truth.description`?",
        "Yes: the stated impact or symptom matches the ground truth.",
        "No: the stated impact conflicts with the ground truth, or none is given.",
    ),
}


def judge_mode() -> str:
    """The judge mode from ``JUDGE_MODE`` (``llm`` when unset)."""
    mode = os.environ.get("JUDGE_MODE", "").strip().lower() or "llm"
    if mode not in MODES:
        raise ValueError(f"JUDGE_MODE must be one of {', '.join(MODES)}, got {mode!r}")
    return mode


def question_key(qid: str) -> str:
    """'D1-Q1' -> 'd1_q1', the answer key in the Jev request."""
    return qid.lower().replace("-", "_")


def parse_expectation(text: str) -> dict[str, str]:
    """'[fault_spec] component=X; namespace=Y || description' -> {component, namespace, description}."""
    spec, sep, desc = text.partition("||")
    if not sep:
        return {"description": text.strip()}
    out = {}
    for part in re.sub(r"^\s*\[fault_spec\]\s*", "", spec).split(";"):
        key, eq, value = part.partition("=")
        if eq and value.strip():
            out[key.strip()] = value.strip()
    out["description"] = desc.strip()
    return out


@dataclass
class JevVerdict:
    """Jev's answers for one diagnosis and what the frozen rule makes of them."""

    score: float
    p: dict[str, float]  # P(yes) per checklist question id
    dimensions: dict[str, float] = field(default_factory=dict)  # mean P(yes) per dimension
    passed: bool | None = None  # Jev's own verdict; None when the LLM judge decides
    send_to_llm: bool = False


class JevAsAJudge:
    """Asks Jev the checklist for one diagnosis and applies the frozen thresholds of ``jev_as_a_judge.yaml``."""

    def __init__(self, client=None, config_path: str | Path | None = None, checklist_path: str | Path | None = None):
        """Load the frozen thresholds and the checklist; ``client`` replaces the TypeSafe client (for tests)."""
        self._client = client
        with open(config_path or JEV_AS_A_JUDGE_CONFIG) as f:
            self.config = yaml.safe_load(f)
        with open(checklist_path or CHECKLIST) as f:
            checklist = yaml.safe_load(f)
        self._dimensions = [(d["id"], [q["id"] for q in d["questions"]]) for d in checklist["dimensions"]]
        self._weights = checklist["scoring"]["weights"]
        self.model = os.environ.get("JUDGE_JEV_MODEL", "").strip() or self.config["model"]

    @property
    def client(self):
        """Lazily create the TypeSafe client, so ``llm`` runs never need the SDK or a key."""
        if self._client is None:
            from typesafe_sdk import RetryPolicy, TypeSafeClient

            self._client = TypeSafeClient(
                model=self.model,
                timeout=float(os.environ.get("JUDGE_JEV_TIMEOUT", "90")),
                retry=RetryPolicy(max_retries=3, backoff_max=10.0, timeout=240.0),
            )
        return self._client

    def questions(self) -> dict:
        """The Noul questions of one request: the 9 checklist questions in checklist order."""
        from typesafe_sdk import Noul

        out = {}
        for _, qids in self._dimensions:
            for qid in qids:
                instructions, true, false = QUESTIONS[qid]
                out[question_key(qid)] = Noul(
                    instructions=f"{instructions} {RULES}", criteria={"true": true, "false": false}
                )
        return out

    def ask(self, solution: str, expectation: str) -> dict[str, float]:
        """P(yes) per checklist question id, from one Jev request."""
        state = {"ground_truth": parse_expectation(expectation), "diagnosis": solution}
        response = self.client.system_one(state, self.questions())
        return {qid: float(response.answers[question_key(qid)].noul) for _, qids in self._dimensions for qid in qids}

    def score(self, p: dict[str, float]) -> tuple[float, dict[str, float]]:
        """The checklist-weighted score (rounded to 4 places) and the mean P(yes) per dimension."""
        dims = {did: sum(p[q] for q in qids) / len(qids) for did, qids in self._dimensions}
        return round(sum(self._weights[did] * v for did, v in dims.items()), 4), dims

    def decide(self, p: dict[str, float], mode: str) -> JevVerdict:
        """Apply the frozen rule of ``mode`` (``jev`` or ``mixed``) to Jev's answers."""
        score, dims = self.score(p)
        verdict = JevVerdict(score=score, p=p, dimensions=dims)
        if mode == "jev":
            verdict.passed = score >= self.config["cut_jev_alone"]
        elif mode == "mixed":
            low, high = self.config["band_low"], self.config["band_high"]
            verdict.send_to_llm = low <= score <= high
            verdict.passed = None if verdict.send_to_llm else score > high
        else:
            raise ValueError(f"JevAsAJudge decides only in jev or mixed mode, got {mode!r}")
        return verdict

    def judge(self, solution: str, expectation: str, mode: str) -> JevVerdict:
        """Ask Jev about one diagnosis and decide; an empty diagnosis fails without a request."""
        if not solution or not solution.strip():
            p = {qid: 0.0 for _, qids in self._dimensions for qid in qids}
            verdict = self.decide(p, mode)
            verdict.passed, verdict.send_to_llm = False, False  # as the LLM judge does for an empty answer
            return verdict
        return self.decide(self.ask(solution, expectation), mode)
