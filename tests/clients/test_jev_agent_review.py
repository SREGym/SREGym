"""LLM review of the origin before the report."""

from __future__ import annotations

import json

from clients.jev_agent import review
from tests.clients.test_jev_agent_loop import FakeLLM, _inv_result, _nb_with, make_ctx


def test_review_switches_to_a_listed_candidate_and_keeps_its_reason():
    inv = _inv_result()
    inv.origin_probs = {"deployment/frontend": 0.5, "deployment/checkout": 0.3, "not_yet_known": 0.2}
    llm = FakeLLM(
        [json.dumps({"origin": "deployment/checkout", "reason": "O3 names it", "mechanism": "checkout rejects calls"})]
    )
    rev = review.review_origin(llm, make_ctx(), _nb_with(inv.observations), inv)
    assert (rev.before, rev.after) == ("deployment/frontend", "deployment/checkout")
    assert rev.mechanism == "checkout rejects calls" and rev.candidates == [
        "deployment/frontend",
        "deployment/checkout",
    ]
    system, user = llm.prompts[0]
    assert "classifier_probability" in user and "not_yet_known" not in json.loads(user)["candidates"][0]["id"]


def test_review_keeps_the_original_on_an_unlisted_or_broken_answer():
    inv = _inv_result()
    inv.origin_probs = {"deployment/frontend": 0.5, "deployment/checkout": 0.3}
    rev = review.review_origin(
        FakeLLM([json.dumps({"origin": "deployment/elsewhere"})]), make_ctx(), _nb_with(inv.observations), inv
    )
    assert rev.after == "deployment/frontend" and "not one of the candidates" in rev.error
    rev = review.review_origin(FakeLLM([RuntimeError("down")]), make_ctx(), _nb_with(inv.observations), inv)
    assert rev.after == "deployment/frontend" and rev.error.startswith("RuntimeError")


def test_review_adds_a_candidate_the_evidence_ranks_above_the_classifier_top():
    from types import SimpleNamespace

    from clients.jev_agent import review
    from clients.jev_agent.notebook import Notebook
    from clients.jev_agent.types import AgentContext, ToolResult

    comps = {
        f"deployment/{n}": {"kind": "Deployment", "name": n, "namespace": "ns"} for n in ("a", "b", "c", "d", "api")
    }
    ctx = AgentContext(app_info={}, namespaces=["ns"], snapshot=SimpleNamespace(components=comps), overview={})
    nb = Notebook({"app_name": "x"}, {"components": []}, scores={})
    nb.add(
        1,
        "config_and_changes",
        "deployment/api",
        ToolResult(
            lines=[
                "Secret db (read into environment variables) was written 2s after container api of pod api-1 started: that "
                "container still holds the values from before the write"
            ]
        ),
    )
    probs = {
        "deployment/a": 0.4,
        "deployment/b": 0.3,
        "deployment/c": 0.2,
        "deployment/d": 0.06,
        "deployment/api": 0.04,
    }
    inv = SimpleNamespace(origin_probs=probs, origin="deployment/a")
    assert review.candidates(inv) == ["deployment/a", "deployment/b", "deployment/c", "deployment/d"]
    assert review.candidates(inv, ctx, nb)[-1] == "deployment/api"
