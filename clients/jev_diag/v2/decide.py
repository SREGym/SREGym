"""The decision: one pick-one question over the top complete chains, asked twice with the order shuffled.

The model never sees fixed labels: each ask relabels the options (o1, o2, ...) in a different order, so neither a
name nor a position can carry an answer. Probabilities are mapped back to explanation keys and averaged.
"""

from __future__ import annotations

import random
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from typesafe_sdk import Choice

from clients.jev_diag.classifier import (
    CATEGORY_QUESTION,
    EVIDENCE_QUESTION,
    MAX_EVIDENCE_ITEMS,
    build_characterization_questions,
)
from clients.jev_diag.v2.explain import Explanation

NONE = "none_of_these"
TOP_K = 6
CONFIDENT_P = 0.6  # averaged probability the leader needs to be accepted without another check
QUESTION = "best_explanation"
# v1's investigation asks the category with this rule; its separate characterization request lacks it.
LIMIT_RULE = (
    "A limit is the cause only if the limit is too low for the workload's normal needs; when a recent change "
    "(see `component.template_changes`) makes the workload consume more, the change is the cause."
)


@dataclass
class Pick:
    """The averaged outcome of the shuffled asks."""

    probabilities: dict[str, float]  # explanation key (or NONE) -> averaged probability
    picks: list[str] = field(default_factory=list)  # the winner of each ask

    @property
    def leader(self) -> str:
        return max(self.probabilities, key=self.probabilities.get)

    @property
    def agreed(self) -> bool:
        return len(set(self.picks)) == 1

    def route(self) -> str:
        """accept | check (unsure: separate the top two) | coverage (none of these won: look wider)."""
        if self.leader == NONE:
            return "coverage"
        if self.agreed and self.probabilities[self.leader] >= CONFIDENT_P:
            return "accept"
        return "check"


def shortlist(explanations: list[Explanation], k: int = TOP_K) -> list[Explanation]:
    """Uncontradicted chains first, then by mechanism-specific support, all supported groups and observed impact;
    stable for ties."""
    ranked = sorted(
        enumerate(explanations),
        key=lambda ie: (
            ie[1].contradicted(),
            -ie[1].specific_groups(),
            -ie[1].supported_groups(),
            -len(ie[1].impact),
            ie[0],
        ),
    )
    return [e for _, e in ranked[:k]]


def question(order: list[str], texts: dict[str, str]) -> tuple[dict[str, Choice], dict[str, str]]:
    """The pick-one question for one ask, with neutral labels in `order`; returns (questions, label -> key)."""
    labels = {f"o{i + 1}": key for i, key in enumerate(order)}
    criteria = {label: texts[key] for label, key in labels.items()}
    q = Choice(
        instructions={
            "question": "Which explanation best accounts for all the observed symptoms in `evidence`?",
            "how_to_decide": [
                "Prefer the explanation whose chain is supported by the observed facts and explains the most "
                "symptoms; an explanation contradicted by a fact is unlikely.",
                "A component that only fails because something it calls fails is a victim, not the origin.",
                f"Choose `{next(lbl for lbl, key in labels.items() if key == NONE)}` when none of the explanations "
                "fits the evidence.",
            ],
        },
        criteria=criteria,
    )
    return {QUESTION: q}, labels


def pick(
    ask: Callable[[str, dict, dict], Any],
    state: dict,
    candidates: list[Explanation],
    *,
    rng: random.Random,
    asks: int = 2,
) -> Pick:
    """Ask the pick-one question `asks` times with shuffled options and average the probabilities."""
    texts = {e.key: e.option_text() for e in candidates}
    texts[NONE] = "None of these explanations fits the evidence."
    keys = list(texts)
    totals = dict.fromkeys(keys, 0.0)
    picks: list[str] = []
    for n in range(asks):
        order = keys[:]
        rng.shuffle(order)
        questions, labels = question(order, texts)
        response = ask(f"decide:{n + 1}", state, questions)
        answer = response.choices[QUESTION]
        for label, p in (answer.probabilities or {}).items():
            if label in labels:
                totals[labels[label]] += float(p) / asks
        picks.append(labels.get(answer.choice, NONE))
    return Pick(probabilities=totals, picks=picks)


@dataclass
class Characterization:
    """v1's characterization of one component: the fact that reveals the mechanism and the fault category."""

    fact: str | None = None
    fact_probability: float = 0.0
    category: str | None = None
    category_probability: float = 0.0


def characterization_questions(items: list[dict]) -> dict[str, Choice]:
    """v1's characterization request, with v1's limit-vs-change rule added to the category question."""
    questions = build_characterization_questions(items)
    category = questions[CATEGORY_QUESTION]
    how = [*category.instructions["how_to_decide"], LIMIT_RULE]
    questions[CATEGORY_QUESTION] = Choice(
        instructions={**category.instructions, "how_to_decide": how}, criteria=category.criteria
    )
    return questions


def characterize(
    ask: Callable[[str, dict, dict], Any], state: dict, facts: list[str], *, rng: random.Random, asks: int = 2
) -> Characterization:
    """v1's characterization request (fault category + "which single item most directly reveals the mechanism?"),
    asked `asks` times with the facts in shuffled order under neutral ids; probabilities are averaged."""
    facts = list(dict.fromkeys(facts))[:MAX_EVIDENCE_ITEMS]
    fact_totals = dict.fromkeys(facts, 0.0)
    category_totals: dict[str, float] = {}
    for n in range(asks):
        order = facts[:]
        rng.shuffle(order)
        items = [{"id": f"f{i + 1}", "text": text} for i, text in enumerate(order)]
        by_id = {item["id"]: item["text"] for item in items}
        response = ask(f"characterize:{n + 1}", {**state, "evidence": items}, characterization_questions(items))
        choices = response.choices
        if EVIDENCE_QUESTION in choices:
            for label, prob in (choices[EVIDENCE_QUESTION].probabilities or {}).items():
                if label in by_id:
                    fact_totals[by_id[label]] += float(prob) / asks
        if CATEGORY_QUESTION in choices:
            for label, prob in (choices[CATEGORY_QUESTION].probabilities or {}).items():
                category_totals[label] = category_totals.get(label, 0.0) + float(prob) / asks
    out = Characterization()
    if fact_totals:
        out.fact = max(fact_totals, key=fact_totals.get)
        out.fact_probability = round(fact_totals[out.fact], 4)
    if category_totals:
        out.category = max(category_totals, key=category_totals.get)
        out.category_probability = round(category_totals[out.category], 4)
    return out
