"""jev_diag v2 decision step and report: shortlist order, shuffled double ask, routing, and report wording."""

import random
from types import SimpleNamespace

from typesafe_sdk import ChoiceAnswer

from clients.jev_diag.v2.decide import NONE, QUESTION, pick, shortlist
from clients.jev_diag.v2.explain import CONTRADICTS, PARTIAL, SUPPORTS, Explanation, Prediction
from clients.jev_diag.v2.report import build_report

REGEX = Explanation(
    key="change:regex",
    pattern="change",
    origin="deployment/store/edge-proxy",
    mechanism="The WAF regex added in the latest revision makes each request cost much more CPU.",
    values=["FILTER_RULE added: rule-v2"],
    impact=["deployment/store/frontend"],
    predictions=[Prediction("CPU rises only on requests that reach the filter", SUPPORTS, group="cpu")],
)
LIMIT = Explanation(
    key="change:limit",
    pattern="change",
    origin="deployment/store/edge-proxy",
    mechanism="The CPU limit set in the latest revision is too low.",
    values=["CPU limit: unset -> 100m"],
    predictions=[Prediction("CPU is high on every request", CONTRADICTS, group="cpu")],
)


def _asker(*answers):
    """A decision stand-in: answers each ask by explanation key, whatever label the option got."""
    sent = []

    def ask(label, state, questions):
        q = questions[QUESTION]
        sent.append(list(q.criteria))
        by_key = answers[len(sent) - 1]
        texts = {"change:regex": REGEX.option_text(), "change:limit": LIMIT.option_text()}
        probs = {}
        for lbl, text in q.criteria.items():
            key = next((k for k, t in texts.items() if t == text), NONE)
            probs[lbl] = by_key.get(key, 0.0)
        top = max(probs, key=probs.get)
        return SimpleNamespace(choices={QUESTION: ChoiceAnswer(choice=top, confidence=0.5, probabilities=probs)})

    return ask, sent


def test_shortlist_puts_contradicted_chains_last():
    assert [e.key for e in shortlist([LIMIT, REGEX])] == ["change:regex", "change:limit"]


def test_shortlist_ranks_mechanism_evidence_above_generic_support():
    generic = Explanation(
        key="change:a",
        pattern="change",
        origin="deployment/shop/a",
        mechanism="m",
        impact=["x", "y"],
        predictions=[Prediction("before", SUPPORTS, group="timing:a"), Prediction("sym", SUPPORTS, group="symptoms:a")],
    )
    specific = Explanation(
        key="stale:b",
        pattern="hidden-state",
        origin="deployment/shop/b",
        mechanism="m",
        predictions=[Prediction("rejected", SUPPORTS, group="rejected:b")],
    )
    assert [e.key for e in shortlist([generic, specific])] == ["stale:b", "change:a"]


def test_pick_averages_shuffled_asks_and_accepts_a_confident_leader():
    ask, sent = _asker({"change:regex": 0.9, "change:limit": 0.1}, {"change:regex": 0.7, "change:limit": 0.3})
    result = pick(ask, {}, [REGEX, LIMIT], rng=random.Random(1))
    assert result.probabilities["change:regex"] == 0.8
    assert result.agreed and result.route() == "accept"
    assert len(sent) == 2 and all(label.startswith("o") for label in sent[0])


def test_none_of_these_routes_to_a_wider_look_and_disagreement_to_a_check():
    ask, _ = _asker({NONE: 0.9}, {NONE: 0.8})
    assert pick(ask, {}, [REGEX, LIMIT], rng=random.Random(2)).route() == "coverage"
    ask, _ = _asker({"change:regex": 0.6, "change:limit": 0.4}, {"change:regex": 0.4, "change:limit": 0.6})
    assert pick(ask, {}, [REGEX, LIMIT], rng=random.Random(3)).route() == "check"


def test_report_names_origin_values_and_only_observed_impact():
    text = build_report([REGEX], observed=["edge-proxy CPU at its 100m limit"])
    assert "Deployment" not in text and "deployment `edge-proxy` in namespace `store`" in text
    assert "rule-v2" in text and "Observed impact" not in text  # one-component cause: callers are evidence
    assert "- deployment `frontend` in namespace `store` fails as a result" in text
    dep = Explanation(
        key="d",
        pattern="dependency",
        origin="deployment/shop/media",
        mechanism="media rejects calls",
        impact=["deployment/shop/media", "deployment/shop/compose"],
    )
    text = build_report([dep], observed=[], key_fact="FailedScheduling: no free ports")
    assert "Mechanism: FailedScheduling: no free ports." in text and "Key evidence" not in text
    assert "Observed impact: deployment `media` in namespace `shop`." in text
    partial = Explanation(key="p", pattern=PARTIAL, origin="mutatingwebhookconfiguration/admission-hook", mechanism="")
    assert "not established" in build_report([partial], observed=[], uncertain=True)
    assert "mutatingwebhookconfiguration `admission-hook`" in build_report([partial], observed=[])
