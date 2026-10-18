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

from clients.jev_diag.v2.explain import Explanation

NONE = "none_of_these"
TOP_K = 6
CONFIDENT_P = 0.6  # averaged probability the leader needs to be accepted without another check
QUESTION = "best_explanation"


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
