"""jev_agent policy loop, escalation, notebook, and report, with a fake decision client and a fake tool layer."""

from __future__ import annotations

import json
from dataclasses import dataclass
from types import SimpleNamespace

import msgspec
import pytest
from typesafe_sdk import Choice, ChoiceAnswer, Noul, NoulAnswer

from clients.jev_agent import escalate as esc
from clients.jev_agent import llm as llm_mod
from clients.jev_agent.config import AgentConfig, load
from clients.jev_agent.notebook import Notebook
from clients.jev_agent.policy import (
    CLUSTER_OBJECT,
    NOT_YET_KNOWN,
    Q_ABNORMAL,
    Q_CATEGORY,
    Q_ENOUGH,
    Q_ERROR_PREFIX,
    Q_NEXT,
    Q_ORIGIN,
    SUBMIT,
    DecisionAsker,
    InvestigationResult,
    Investigator,
    build_options,
    build_step_questions,
)
from clients.jev_agent.report import build_report, template_text
from clients.jev_agent.types import CLUSTER_GROUP, COMPONENT, AgentContext, Observation, ToolResult, ToolSpec
from clients.jev_diag.classifier import FAULT_CATEGORIES
from clients.jev_diag.collector import estimate_tokens

NS = "shop"
COMPS = ["deployment/frontend", "deployment/cart", "deployment/checkout", "deployment/db", "deployment/idle"]
SCORES = {
    "deployment/frontend": 5.0,
    "deployment/cart": 3.0,
    "deployment/checkout": 2.0,
    "deployment/db": 1.0,
    "deployment/idle": 0.0,
}


# --------------------------------------------------------------------------- fakes


def fake_tools(*, names=None, mcp_tools=(), inapplicable=(), lines=None):
    """A tool layer stand-in: component tools over COMPS and a cluster_objects tool over groups."""
    names = names or [
        "describe",
        "logs",
        "metric_series_cpu",
        "metric_series_memory",
        "metric_series_rpc",
        "call_edges",
        "config_and_changes",
    ]
    calls: list[tuple[str, str]] = []

    def runner(name):
        def run(ctx, target):
            return ToolResult(lines=(lines or {}).get((name, target), [f"{name} on {target}: ok"]))

        return run

    registry = {
        name: ToolSpec(
            name=name,
            target_kind=COMPONENT,
            reveals=f"Reveals {name}.",
            run=runner(name),
            applicable=(lambda n: (lambda ctx, t: (n, t) not in inapplicable))(name),
            uses_mcp=name in mcp_tools,
        )
        for name in names
    }
    registry["cluster_objects"] = ToolSpec(
        name="cluster_objects",
        target_kind=CLUSTER_GROUP,
        reveals="Lists cluster objects.",
        run=runner("cluster_objects"),
    )

    def candidate_targets(ctx, spec):
        if spec.target_kind == CLUSTER_GROUP:
            return ["admission", "quota", "network", "rbac", "dns", "nodes", "storage"]
        return list(COMPS)

    def run(ctx, name, target):
        calls.append((name, target))
        return registry[name].run(ctx, target)

    return SimpleNamespace(REGISTRY=registry, candidate_targets=candidate_targets, run=run, calls=calls)


def fake_overview_mod():
    return SimpleNamespace(
        component_scores=lambda ov: dict(SCORES),
        neighbors=lambda ov, cid: ["deployment/checkout", "deployment/idle"],
        render=lambda ov, budget_tokens=4000: {"components": []},
    )


def make_ctx(mcp_ok=True) -> AgentContext:
    comps = {cid: {"kind": "Deployment", "name": cid.split("/")[1], "namespace": NS} for cid in COMPS}
    return AgentContext(
        app_info={"app_name": "shop", "namespaces": [NS], "descriptions": "a shop"},
        namespaces=[NS],
        snapshot=SimpleNamespace(components=comps),
        overview={"components": {}},
        mcp_ok=mcp_ok,
    )


def make_notebook(overview=None) -> Notebook:
    overview = overview or {
        "components": [
            {"id": "deployment/frontend", "ready": "0/1", "restarts": 3},
            {"id": "deployment/idle", "ready": "1/1"},
        ]
    }
    return Notebook({"app_name": "shop", "namespaces": [NS]}, overview, scores=dict(SCORES))


@dataclass
class FakeUsage:
    input_tokens: int = 100
    output_tokens: int = 5


@dataclass
class FakeResponse:
    answers: dict
    model: str = "fake-decisions"
    usage: FakeUsage = None  # type: ignore[assignment]

    def __post_init__(self):
        if self.usage is None:
            self.usage = FakeUsage()

    @property
    def choices(self):
        return {k: v for k, v in self.answers.items() if isinstance(v, ChoiceAnswer)}

    @property
    def nouls(self):
        return {k: v for k, v in self.answers.items() if isinstance(v, NoulAnswer)}


def choice(values, pick, p):
    values = list(values)
    rest = (1.0 - p) / max(1, len(values) - 1)
    probs = {v: (p if v == pick else rest) for v in values}
    return ChoiceAnswer(choice=pick, confidence=p, probabilities=probs)


def answer(
    questions,
    *,
    nxt=None,
    next_p=0.6,
    abnormal=0.8,
    enough=0.2,
    origin=NOT_YET_KNOWN,
    origin_p=0.5,
    category="config_value",
    support=0.9,
    error=None,
):
    out = {}
    for name, q in questions.items():
        if name == Q_NEXT:
            keys = list(q.criteria)
            pick = nxt(keys) if callable(nxt) else (nxt or keys[0])
            out[name] = choice(keys, pick, next_p)
        elif name == Q_ABNORMAL:
            out[name] = NoulAnswer(noul=abnormal)
        elif name == Q_ENOUGH:
            out[name] = NoulAnswer(noul=enough)
        elif name == Q_ORIGIN:
            out[name] = choice(q.criteria, origin, origin_p)
        elif name == Q_CATEGORY:
            out[name] = choice(q.criteria, category, 0.7)
        elif name.startswith(Q_ERROR_PREFIX) and error is not None:
            out[name] = choice(q.criteria, error(q.instructions) if callable(error) else error, 0.9)
        elif name.startswith("supports_"):
            out[name] = NoulAnswer(noul=support(name) if callable(support) else support)
    return FakeResponse(out)


class FakeClient:
    """Answers each request with script(call_index, state, questions) -> dict of answer() kwargs."""

    def __init__(self, script):
        self.script = script
        self.calls: list[tuple[dict, dict]] = []

    def system_one(self, state, questions, **kwargs):
        idx = len(self.calls)
        self.calls.append((state, questions))
        return answer(questions, **self.script(idx, state, questions))


class FakeLLM:
    def __init__(self, texts):
        self.texts = list(texts)
        self.calls = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.prompts: list[tuple[str, str]] = []
        self.model = "fake-llm"

    def _next(self, system, user):
        self.calls += 1
        self.prompts.append((system, user))
        text = self.texts.pop(0)
        if isinstance(text, Exception):
            raise text
        return llm_mod.LLMResponse(text=text, usage={"input_tokens": 10, "output_tokens": 10})

    def complete(self, system, user, **kw):
        return self._next(system, user)

    def complete_json(self, system, user, **kw):
        resp = self._next(system, user)
        resp.data = llm_mod.parse_json_object(resp.text)
        return resp


def cfg(**kw) -> AgentConfig:
    base = dict(max_steps=6, min_steps=2, wall_budget_s=1000, max_options=40, per_tool=4, state_budget=24000)
    base.update(kw)
    return AgentConfig(**base)


def investigator(script, *, tools=None, config=None, llm=None, clock=None, notebook=None):
    tools = tools or fake_tools()
    kwargs = {}
    if clock is not None:
        kwargs["clock"] = clock
    inv = Investigator(
        make_ctx(),
        FakeClient(script),
        notebook or make_notebook(),
        None,
        llm,
        config=config or cfg(),
        tools=tools,
        overview=fake_overview_mod(),
        **kwargs,
    )
    return inv, tools


# --------------------------------------------------------------------------- config


def test_config_defaults_and_env(monkeypatch):
    for name in ("JEV_AGENT_MAX_STEPS", "JEV_AGENT_POLICY", "JEV_AGENT_LLM_MODEL"):
        monkeypatch.delenv(name, raising=False)
    c = load()
    assert (c.max_steps, c.min_steps, c.wall_budget_s, c.policy, c.llm_model) == (30, 2, 300.0, "model", "")
    assert (c.max_llm_calls, c.max_options, c.per_tool, c.state_budget) == (2, 40, 4, 24000)
    monkeypatch.setenv("JEV_AGENT_MAX_STEPS", "5")
    monkeypatch.setenv("JEV_AGENT_POLICY", "scripted")
    assert load().max_steps == 5 and load().policy == "scripted"
    monkeypatch.setenv("JEV_AGENT_POLICY", "bogus")
    with pytest.raises(ValueError):
        load()


# --------------------------------------------------------------------------- options


def test_options_respect_per_tool_quota_cap_and_submit_last():
    tools = fake_tools()
    nb = make_notebook()
    opts = build_options(make_ctx(), nb, tools, SCORES, cfg(per_tool=2, max_options=10))
    keys = list(opts)
    assert keys[-1] == SUBMIT
    assert len(keys) == 11  # 10 + submit_now
    per_tool = {}
    for k in keys[:-1]:
        per_tool[k.split(":")[0]] = per_tool.get(k.split(":")[0], 0) + 1
    assert max(per_tool.values()) <= 2
    # round-robin: each tool's best target comes first, ordered by component score
    assert all(k.endswith("deployment/frontend") or k.startswith("cluster_objects") for k in keys[:8])
    assert opts["describe:deployment/frontend"].startswith("Run describe on deployment/frontend. Known about")
    from clients.jev_agent.policy import tool_glossary

    glossary = tool_glossary(tools, opts)
    assert glossary.count("describe: Reveals describe") == 1  # each tool's description once, not per option
    assert "not yet examined" in opts["describe:deployment/frontend"]
    assert "restarts=3" in opts["describe:deployment/frontend"]


def test_options_full_quota_and_global_cap():
    opts = build_options(make_ctx(), make_notebook(), fake_tools(), SCORES, cfg(per_tool=4, max_options=40))
    assert len(opts) == 8 * 4 + 1  # 8 tools x 4 targets + submit_now
    counts = {}
    for k in opts:
        counts[k.split(":")[0]] = counts.get(k.split(":")[0], 0) + 1
    assert counts["logs"] == 4 and counts["cluster_objects"] == 4
    assert "describe:deployment/idle" not in opts  # lowest score cut by the per-tool quota
    capped = build_options(make_ctx(), make_notebook(), fake_tools(), SCORES, cfg(per_tool=4, max_options=20))
    assert len(capped) == 21
    assert all(k.split(":")[0] in counts for k in capped)
    assert "describe:deployment/db" not in capped  # fourth-ranked targets are the first to go


def test_options_dedup_mcp_and_applicable_filters():
    tools = fake_tools(mcp_tools=("metric_series_cpu",), inapplicable={("logs", "deployment/frontend")})
    nb = make_notebook()
    nb.actions_done.add("describe:deployment/frontend")
    opts = build_options(make_ctx(mcp_ok=False), nb, tools, SCORES, cfg())
    assert "describe:deployment/frontend" not in opts
    assert "logs:deployment/frontend" not in opts
    assert not any(k.startswith("metric_series_cpu:") for k in opts)
    assert "describe:deployment/cart" in opts
    opts_mcp = build_options(make_ctx(mcp_ok=True), nb, tools, SCORES, cfg())
    assert "metric_series_cpu:deployment/frontend" in opts_mcp


def test_suggested_actions_pinned_first_and_invalid_ones_ignored():
    nb = make_notebook()
    nb.actions_done.add("logs:deployment/db")
    suggested = [
        {"tool": "logs", "target": "deployment/db", "reason": "already done"},
        {"tool": "config_and_changes", "target": "deployment/db", "reason": "check the change"},
        {"tool": "nope", "target": "deployment/db", "reason": "off menu"},
    ]
    opts = build_options(make_ctx(), nb, fake_tools(), SCORES, cfg(), suggested)
    first = next(iter(opts))
    assert first == "config_and_changes:deployment/db"
    assert opts[first].startswith("(suggested)") and "check the change" in opts[first]
    assert "logs:deployment/db" not in opts


def test_step_questions_shape_and_serialisation():
    nb = make_notebook()
    nb.add(1, "logs", "deployment/frontend", ToolResult(lines=["panic: boom"]))
    opts = build_options(make_ctx(), nb, fake_tools(), SCORES, cfg())
    qs = build_step_questions(make_ctx(), nb, opts, SCORES)
    assert set(qs) == {Q_NEXT, Q_ABNORMAL, Q_ENOUGH, Q_ORIGIN, Q_CATEGORY}
    assert isinstance(qs[Q_NEXT], Choice) and isinstance(qs[Q_ABNORMAL], Noul)
    assert "O1" in qs[Q_ABNORMAL].instructions
    origins = set(qs[Q_ORIGIN].criteria)
    assert set(COMPS) | {CLUSTER_OBJECT, NOT_YET_KNOWN} <= origins
    # input data is a candidate origin only for components that consume from a queue or stream
    assert not {o for o in origins if o.startswith("data/")}
    assert not origins - set(COMPS) - {CLUSTER_OBJECT, NOT_YET_KNOWN} - {o for o in origins if o.count("/") == 2}
    assert set(qs[Q_CATEGORY].criteria) == set(FAULT_CATEGORIES)
    json.dumps({k: msgspec.to_builtins(q) for k, q in qs.items()})
    nb.observations[-1].abnormal = 0.9
    assert Q_ABNORMAL not in build_step_questions(make_ctx(), nb, opts, SCORES)


# --------------------------------------------------------------------------- stopping


def test_stop_confident_twice_requires_same_origin_and_records_abnormal():
    def script(i, state, qs):
        if i == 0:
            return dict(origin="deployment/cart", origin_p=0.7, enough=0.8)
        if i == 1:
            return dict(origin="deployment/frontend", origin_p=0.7, enough=0.8, abnormal=0.9)
        return dict(origin="deployment/frontend", origin_p=0.7, enough=0.8, abnormal=0.3)

    inv, tools = investigator(script)
    res = inv.run()
    # step1 (0 tools) cannot stop; step2 (1 tool) cannot stop; step3 (2 tools) stops: same origin twice
    assert res.stop_reason == "confident_twice"
    assert res.origin == "deployment/frontend"
    assert res.tool_steps == 2 and len(tools.calls) == 2
    assert [o.abnormal for o in res.observations] == [0.9, 0.3]


def test_stop_confident_once_but_not_before_min_steps():
    inv, tools = investigator(lambda i, s, q: dict(origin="deployment/db", origin_p=0.9, enough=0.9))
    res = inv.run()
    assert res.stop_reason == "confident_once"
    assert res.tool_steps == 2  # min steps
    assert res.decision_calls == 3


def test_stop_thresholds_come_from_the_config():
    # 0.9 is below a raised origin_p_once, so the same answer stops one step later, on two steps in a row
    inv, _ = investigator(
        lambda i, s, q: dict(origin="deployment/db", origin_p=0.9, enough=0.9), config=cfg(origin_p_once=0.95)
    )
    res = inv.run()
    assert res.stop_reason == "confident_twice"


def test_not_yet_known_is_ignored_for_confidence():
    # not_yet_known carries 0.9; the best real origin has only 0.025 -> never stops early
    inv, _ = investigator(lambda i, s, q: dict(origin=NOT_YET_KNOWN, origin_p=0.9, enough=0.9), config=cfg(max_steps=3))
    res = inv.run()
    assert res.stop_reason == "max_steps"
    assert res.origin != NOT_YET_KNOWN


def test_submit_now_stops_only_after_min_steps_and_with_p():
    def script(i, s, q):
        return dict(nxt=SUBMIT, next_p=0.6 if i >= 2 else 0.9)

    inv, tools = investigator(script)
    res = inv.run()
    assert res.stop_reason == "submit_now"
    assert res.tool_steps == 2
    # before min steps the most probable non-submit option was executed instead
    assert res.steps[0]["chosen"] == SUBMIT and res.steps[0]["executed"] != SUBMIT


def test_submit_now_with_low_probability_continues_once_then_stops_when_repeated():
    inv, _ = investigator(lambda i, s, q: dict(nxt=SUBMIT, next_p=0.4, abnormal=0.2), config=cfg(max_steps=6))
    res = inv.run()
    # min_steps tool steps run first; then two consecutive submit_now picks end the investigation
    assert res.stop_reason == "submit_twice"
    assert res.tool_steps < 6


def test_max_steps_ends_with_a_concluding_request():
    inv, _ = investigator(
        lambda i, s, q: dict(origin="deployment/cart", origin_p=0.5, abnormal=0.2), config=cfg(max_steps=3)
    )
    res = inv.run()
    assert res.stop_reason == "max_steps"
    assert res.tool_steps == 3
    assert res.decision_calls == 4
    last = res.steps[-1]
    assert last["label"].startswith("conclude") and "options" not in last
    assert res.observations[-1].abnormal is not None  # the last observation got judged
    assert res.origin == "deployment/cart"


def test_wall_budget_stops_the_loop():
    t = {"now": 0.0}

    def clock():
        t["now"] += 50.0
        return t["now"]

    inv, _ = investigator(lambda i, s, q: {}, config=cfg(max_steps=20, wall_budget_s=120), clock=clock)
    res = inv.run()
    assert res.stop_reason == "wall_budget"
    assert res.tool_steps < 20


def test_step_trace_records_options_choice_probability_and_rank():
    inv, _ = investigator(
        lambda i, s, q: dict(nxt=lambda keys: keys[2], next_p=0.55, origin="deployment/db", origin_p=0.9, enough=0.9)
    )
    res = inv.run()
    s0 = res.summary()["step_trace"][0]
    assert s0["options"] == 33
    assert s0["chosen_rank"] == 3 and s0["chosen_p"] == pytest.approx(0.55)
    assert s0["executed"] == s0["chosen"]


# --------------------------------------------------------------------------- escalation


def test_escalation_triggers():
    # a flat next_action distribution alone is no reason (it is flat over ~40 options)
    assert esc.trigger([{"next_p": 0.05, "next_entropy": 0.99}], tool_steps=1, max_steps=12) is None
    assert esc.trigger([{"enough": 0.1}], tool_steps=7, max_steps=30) is None
    assert "enough_evidence" in esc.trigger([{"enough": 0.1}], tool_steps=8, max_steps=30)
    stale = [{"next_p": 0.5, "last_abnormal": 0.2}] * 3
    assert "judged normal" in esc.trigger(stale, tool_steps=3, max_steps=12)
    assert esc.trigger(stale[:2], tool_steps=2, max_steps=12) is None
    late = [{"next_p": 0.5, "origin_real_p": 0.3}]
    assert esc.trigger(late, tool_steps=5, max_steps=12) is None
    assert "half the budget" in esc.trigger(late, tool_steps=6, max_steps=12)
    assert (
        esc.trigger([{"next_p": 0.5, "origin_real_p": 0.5, "last_abnormal": 0.9}], tool_steps=6, max_steps=12) is None
    )


def test_normalized_entropy():
    assert esc.normalized_entropy({"a": 0.5, "b": 0.5}) == pytest.approx(1.0)
    assert esc.normalized_entropy({"a": 1.0, "b": 0.0}) == pytest.approx(0.0)


def test_escalation_json_validation_drops_off_menu_items():
    menu = {"logs": {"deployment/cart"}, "describe": {"deployment/cart", "deployment/db"}}
    payload = {
        "hypotheses": [
            {
                "component": "deployment/cart",
                "mechanism": "bad env",
                "supporting": ["O1", "O99"],
                "test": {"tool": "describe", "target": "deployment/cart"},
            },
            {"component": "deployment/db", "mechanism": "x", "test": {"tool": "shell", "target": "rm -rf"}},
            {"mechanism": "no component"},
        ],
        "suggested_actions": [
            {"tool": "logs", "target": "deployment/cart", "reason": "r"},
            {"tool": "logs", "target": "deployment/unknown", "reason": "r"},
            {"tool": "kubectl_exec", "target": "deployment/cart", "reason": "r"},
            {"tool": "describe", "target": "deployment/db", "reason": "done already"},
        ],
    }
    hyps, sugg, dropped = esc.validate(payload, menu, observation_ids={"O1"}, done={"describe:deployment/db"})
    assert [h["component"] for h in hyps] == ["deployment/cart", "deployment/db"]
    assert hyps[0]["supporting"] == ["O1"] and hyps[0]["test"] == "describe:deployment/cart"
    assert hyps[1]["test"] is None
    assert [(s["tool"], s["target"]) for s in sugg] == [("logs", "deployment/cart"), ("describe", "deployment/cart")]
    assert len(dropped) == 5
    assert esc.validate("not json", menu, observation_ids=set()) == ([], [], ["payload is not a JSON object"])


def test_parse_json_object_finds_first_object():
    assert llm_mod.parse_json_object('Sure: {"a": {"b": 1}} trailing {"c": 2}') == {"a": {"b": 1}}
    assert llm_mod.parse_json_object("no json") is None
    assert llm_mod.output_text({"output": [{"content": [{"type": "output_text", "text": "hi"}]}]}) == "hi"


def test_investigator_escalates_once_and_pins_suggestions():
    llm = FakeLLM(
        [
            json.dumps(
                {
                    "hypotheses": [
                        {
                            "component": "deployment/db",
                            "mechanism": "db rejects logins",
                            "supporting": [],
                            "test": {"tool": "config_and_changes", "target": "deployment/db"},
                        }
                    ],
                    "suggested_actions": [{"tool": "logs", "target": "deployment/db", "reason": "look at db"}],
                }
            )
        ]
    )
    # three observations in a row judged normal -> escalate
    inv, tools = investigator(
        lambda i, s, q: dict(next_p=0.1, abnormal=0.1), llm=llm, config=cfg(max_steps=6, max_llm_calls=2)
    )
    res = inv.run()
    assert len(res.escalations) == 1 and llm.calls == 1
    assert res.escalations[0]["suggested"][0] == {"tool": "logs", "target": "deployment/db", "reason": "look at db"}
    assert inv.notebook.hypotheses[0]["id"] == "H1"
    # the next request pinned the suggestions first
    after = res.escalations[0]["step"]
    _, next_questions = inv.asker._diag._client.calls[after]
    keys = list(next_questions[Q_NEXT].criteria)
    assert keys[:2] == ["logs:deployment/db", "config_and_changes:deployment/db"]
    assert next_questions[Q_NEXT].criteria[keys[0]].startswith("(suggested)")
    assert "hypotheses" in inv.asker._diag._client.calls[after][0]
    assert res.summary()["escalated"] is True


def test_no_escalation_when_turned_off():
    llm = FakeLLM([])
    inv, _ = investigator(
        lambda i, s, q: dict(next_p=0.1, abnormal=0.1),
        llm=llm,
        config=cfg(max_steps=6, max_llm_calls=2, escalate=False),
    )
    res = inv.run()
    assert res.escalations == [] and llm.calls == 0


def test_no_escalation_without_spare_llm_call():
    llm = FakeLLM([])
    inv, _ = investigator(lambda i, s, q: dict(next_p=0.1), llm=llm, config=cfg(max_steps=3, max_llm_calls=1))
    res = inv.run()
    assert res.escalations == [] and llm.calls == 0


def test_escalation_llm_error_is_recorded_not_raised():
    llm = FakeLLM([RuntimeError("boom")])
    inv, _ = investigator(lambda i, s, q: dict(next_p=0.1), llm=llm, config=cfg(max_steps=3))
    res = inv.run()
    assert res.escalations[0]["error"].startswith("RuntimeError")


# --------------------------------------------------------------------------- notebook


def test_notebook_compression_order_fits_budget():
    rows = [{"id": cid, "ready": "1/1", "detail": "x" * 400} for cid in COMPS] + [
        {"id": f"deployment/quiet{i}", "ready": "1/1", "detail": "y" * 400} for i in range(10)
    ]
    scores = {**SCORES, **{f"deployment/quiet{i}": 0.0 for i in range(10)}}
    nb = Notebook({"app_name": "shop"}, {"components": rows}, scores=scores)
    for i in range(12):
        o = nb.add(i + 1, "logs", COMPS[i % 4], ToolResult(lines=[f"line {i}-{j} " + "z" * 150 for j in range(8)]))
        if i < 10:
            o.abnormal = 0.9 if i == 1 else 0.2
    full = nb.state(10**6)
    assert "compression" not in full
    budget = estimate_tokens(full) // 3
    st = nb.state(budget)
    assert estimate_tokens(st) <= budget
    assert st["compression"][:3] == [
        "collapse_normal_observations",
        "collapse_older_observations",
        "drop_healthy_overview_rows",
    ]
    by_id = {(o if isinstance(o, str) else o["id"]): o for o in st["observations"]}
    rendered = st["observations"]
    assert isinstance(rendered[0], str) and rendered[0].startswith("O1 ")  # judged normal -> one line
    assert isinstance(rendered[1], dict)  # O2 abnormal keeps a structured entry
    assert isinstance(rendered[-1], dict) and rendered[-1]["id"] == "O12"
    assert by_id
    ids = [r["id"] for r in st["overview"]["components"]]
    assert "deployment/idle" not in ids and "deployment/frontend" in ids


def test_notebook_mild_compression_keeps_overview():
    nb = make_notebook()
    for i in range(3):
        o = nb.add(i + 1, "logs", "deployment/cart", ToolResult(lines=["a" * 300] * 8))
        o.abnormal = 0.1
    full = estimate_tokens(nb.state(10**6))
    st = nb.state(full - 50)
    assert st["compression"] == ["collapse_normal_observations"]
    assert len(st["overview"]["components"]) == 2


def test_notebook_records_errors_and_known():
    nb = make_notebook()
    o = nb.add(1, "logs", "deployment/cart", ToolResult(lines=[], error="timeout"))
    assert o.id == "O1" and o.lines == ["tool error: timeout"]
    o.abnormal = 0.8
    assert nb.known("deployment/cart").startswith("examined: logs (O1, abnormal)")
    assert "logs:deployment/cart" in nb.actions_done


# --------------------------------------------------------------------------- scripted policy


def test_scripted_policy_order_and_final_question():
    inv, tools = investigator(
        lambda i, s, q: dict(origin="deployment/cart", origin_p=0.6), config=cfg(policy="scripted", max_steps=30)
    )
    res = inv.run()
    tools_order = [
        "describe",
        "logs",
        "metric_series_cpu",
        "metric_series_memory",
        "metric_series_rpc",
        "call_edges",
        "config_and_changes",
    ]
    expected = [(t, c) for c in ["deployment/frontend", "deployment/cart", "deployment/checkout"] for t in tools_order]
    expected += [("cluster_objects", g) for g in ["admission", "quota", "network", "rbac", "dns", "nodes", "storage"]]
    assert tools.calls == expected
    assert res.stop_reason == "plan_done" and res.decision_calls == 1
    _, qs = inv.asker._diag._client.calls[0]
    assert set(qs) == {Q_ORIGIN, Q_CATEGORY}
    assert res.origin == "deployment/cart" and res.fault_category == "config_value"


def test_scripted_policy_respects_step_budget():
    inv, tools = investigator(lambda i, s, q: {}, config=cfg(policy="scripted", max_steps=5))
    res = inv.run()
    assert len(tools.calls) == 5 and res.stop_reason == "max_steps"


# --------------------------------------------------------------------------- report and results


def _inv_result(origin="deployment/frontend", obs=None):
    obs = obs or [
        Observation("O1", 1, "logs", "deployment/frontend", ["panic: CART_ADDR not set"], abnormal=0.95),
        Observation("O2", 2, "describe", "deployment/cart", ["ready 1/1"], abnormal=0.1),
        Observation(
            "O3",
            3,
            "logs",
            "deployment/checkout",
            ["error lines name other components: deployment/frontend"],
            abnormal=0.8,
        ),
    ]
    return InvestigationResult(
        origin=origin,
        origin_probs={origin: 0.8},
        fault_category="config_value",
        observations=obs,
        steps=[],
        decision_calls=3,
        tokens={"input_tokens": 300, "output_tokens": 15},
        escalations=[],
        stop_reason="confident_once",
        tool_steps=2,
    )


def _nb_with(obs):
    nb = make_notebook()
    nb.observations = list(obs)
    return nb


def test_report_template_fallback_without_llm():
    inv = _inv_result()
    client = FakeClient(lambda i, s, q: dict(support=lambda name: 0.9 if name == "supports_O1" else 0.1))
    asker = DecisionAsker(client)
    rep = build_report(asker, _nb_with(inv.observations), inv, make_ctx(), overview_mod=fake_overview_mod())
    assert rep.source == "template"
    assert rep.text.startswith("Root cause component: Deployment `frontend` in namespace `shop`.")
    assert "Fault category: config value" in rep.text
    # the origin's own abnormal findings are reported verbatim, whatever the evidence selection said
    assert "Observed at the origin (verbatim):\n- O1 logs deployment/frontend: panic: CART_ADDR not set" in rep.text
    assert "O2" not in rep.text
    assert "Affected components: deployment/checkout." in rep.text
    assert rep.support == {"O1": 0.9, "O2": 0.1, "O3": 0.1}
    _, qs = client.calls[0]
    assert set(qs) == {"supports_O1", "supports_O2", "supports_O3"}


def test_report_uses_llm_with_facts_only_prompt_and_falls_back_on_error():
    inv = _inv_result()
    words = " ".join(["word"] * 130)
    llm = FakeLLM([words])
    asker = DecisionAsker(FakeClient(lambda i, s, q: {}))
    rep = build_report(asker, _nb_with(inv.observations), inv, make_ctx(), llm=llm, overview_mod=fake_overview_mod())
    assert rep.source == "llm" and rep.text == words
    system, user = llm.prompts[0]
    assert "Use only the facts" in system and "namespace" in system and "blast radius" in system
    assert "CART_ADDR" in user and "deployment/checkout" in user
    bad = FakeLLM([RuntimeError("down")])
    rep2 = build_report(asker, _nb_with(inv.observations), inv, make_ctx(), llm=bad, overview_mod=fake_overview_mod())
    assert rep2.source == "template" and rep2.llm_error.startswith("RuntimeError")
    short = FakeLLM(["too short"])
    assert build_report(asker, _nb_with(inv.observations), inv, make_ctx(), llm=short).source == "template"
    spent = FakeLLM([words])
    spent.calls = 2
    assert (
        build_report(asker, _nb_with(inv.observations), inv, make_ctx(), llm=spent, max_llm_calls=2).source
        == "template"
    )


def test_template_for_cluster_object_origin():
    inv = _inv_result(origin=CLUSTER_OBJECT)
    text = template_text(make_ctx(), inv, inv.observations[:1], [])
    assert text.startswith("Root cause: a cluster-level object")


def test_results_summary_fields():
    inv, _ = investigator(lambda i, s, q: dict(origin="deployment/db", origin_p=0.9, enough=0.9))
    res = inv.run()
    summary = res.summary()
    for key in (
        "origin",
        "fault_category",
        "steps",
        "decision_calls",
        "input_tokens",
        "escalated",
        "stop_reason",
        "step_trace",
    ):
        assert key in summary
    assert summary["steps"] == 2 and summary["decision_calls"] == 3 and summary["input_tokens"] == 300
    assert summary["origin"] == "deployment/db" and summary["escalated"] is False
    step = summary["step_trace"][0]
    assert {"options", "chosen", "chosen_p", "chosen_rank", "executed"} <= set(step)


# --------------------------------------------------------------------------- registration


def test_filtered_egress_rules_for_jev_agent():
    from sregym.service.internet_policy import EndpointRule, InternetPolicy
    from sregym.service.provider_endpoints import provider_endpoint_rules

    policy = InternetPolicy.from_mode("filtered", agent_name="jev_agent", model_id="jev-latest")
    assert provider_endpoint_rules(policy, {"JEV_DIAG_BACKEND": "openai_decisions"}) == (
        EndpointRule("api.openai.com", 443),
    )
    assert provider_endpoint_rules(policy, {"JEV_DIAG_BACKEND": "openrouter"}) == (EndpointRule("openrouter.ai", 443),)
    # default LLM backend is the Codex CLI on the ChatGPT subscription
    rules = provider_endpoint_rules(policy, {"JEV_DIAG_BACKEND": "openrouter", "JEV_AGENT_LLM_MODEL": "gpt-6-luna"})
    assert set(rules) == {
        EndpointRule("openrouter.ai", 443),
        EndpointRule("chatgpt.com", 443),
        EndpointRule("auth.openai.com", 443),
    }
    rules = provider_endpoint_rules(
        policy, {"JEV_DIAG_BACKEND": "openrouter", "JEV_AGENT_LLM_MODEL": "gpt-6-luna", "JEV_AGENT_LLM_BACKEND": "api"}
    )
    assert set(rules) == {EndpointRule("openrouter.ai", 443), EndpointRule("api.openai.com", 443)}
    diag = InternetPolicy.from_mode("filtered", agent_name="jev_diag", model_id="jev-latest")
    assert provider_endpoint_rules(diag, {"JEV_DIAG_BACKEND": "openrouter", "JEV_AGENT_LLM_MODEL": "x"}) == (
        EndpointRule("openrouter.ai", 443),
    )


def test_jev_diag_configure_accepts_jev_agent(monkeypatch):
    from clients.jev.config import MODEL_ENV
    from clients.jev_diag.config import configure

    monkeypatch.setenv("JEV_DIAG_BACKEND", "openai_decisions")
    monkeypatch.setenv("DECISIONS_OPENAI_API_KEY", "test")
    # configure() writes MODEL_ENV itself; setting it here first makes monkeypatch restore the original after the test
    monkeypatch.setenv(MODEL_ENV, "")
    args = SimpleNamespace(agent="jev_agent", model="gpt-6-luna", judge_model="judge", use_external_harness=False)
    configure(args)
    import os

    assert os.environ[MODEL_ENV] == "gpt-6-luna"
    with pytest.raises(ValueError, match="--agent jev_agent"):
        configure(SimpleNamespace(agent="jev_agent", model="m", judge_model=None, use_external_harness=False))


# --------------------------------------------------------------------------- llm client (no network)


class _Resp:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body
        self.text = json.dumps(body)

    def json(self):
        return self._body


def test_llm_client_request_shape_retries_and_incomplete(monkeypatch):
    sent = []
    replies = [
        _Resp(400, {"error": {"message": "Unsupported parameter: 'text.format'"}}),
        _Resp(500, {"error": "busy"}),
        _Resp(
            200,
            {
                "model": "m",
                "status": "completed",
                "usage": {"input_tokens": 7, "output_tokens": 3},
                "output": [{"type": "message", "content": [{"type": "output_text", "text": 'x {"a": 1}'}]}],
            },
        ),
        _Resp(
            200,
            {
                "status": "incomplete",
                "incomplete_details": {"reason": "max_output_tokens"},
                "output": [],
                "usage": {"input_tokens": 1, "output_tokens": 9},
            },
        ),
    ]

    def fake_post(url, json=None, headers=None, timeout=None):
        sent.append(json)
        return replies.pop(0)

    monkeypatch.setattr(llm_mod.requests, "post", fake_post)
    monkeypatch.setattr(llm_mod.time, "sleep", lambda s: None)
    client = llm_mod.LLMClient("m", key="k")
    resp = client.complete_json("sys", "user")
    assert resp.data == {"a": 1} and client.tokens == 10 and client.calls == 1
    assert sent[0]["text"] == {"format": {"type": "json_object"}} and sent[0]["reasoning"] == {"effort": "low"}
    assert "text" not in sent[1] and sent[1]["input"][0] == {"role": "system", "content": "sys"}
    with pytest.raises(RuntimeError, match="incomplete"):
        client.complete("sys", "user")
    assert client.calls == 2


def test_make_llm_disabled_without_model_or_key(monkeypatch):
    monkeypatch.delenv("JEV_AGENT_LLM_MODEL", raising=False)
    assert llm_mod.make_llm() is None
    # api backend: disabled without a key
    monkeypatch.setenv("JEV_AGENT_LLM_BACKEND", "api")
    for name in llm_mod.KEY_ENVS:
        monkeypatch.delenv(name, raising=False)
    assert llm_mod.make_llm("gpt-6-luna") is None
    # codex backend (default): disabled when the CLI is missing, enabled when present
    monkeypatch.setenv("JEV_AGENT_LLM_BACKEND", "codex")
    monkeypatch.setattr(llm_mod.shutil, "which", lambda name: None)
    assert llm_mod.make_llm("gpt-6-luna") is None
    monkeypatch.setattr(llm_mod.shutil, "which", lambda name: "/usr/bin/codex")
    assert isinstance(llm_mod.make_llm("gpt-6-luna"), llm_mod.CodexLLMClient)


def test_codex_client_counts_tokens(monkeypatch):
    from llm_backend import judge_bridge

    def fake_run_codex(prompt, model, cwd, env, effort=None, usage=None):
        usage.update(input_tokens=16000, cached_input_tokens=9000, output_tokens=300, reasoning_output_tokens=200)
        return '{"ok": true}'

    monkeypatch.setattr(judge_bridge, "_run_codex", fake_run_codex)
    monkeypatch.setattr(llm_mod.shutil, "which", lambda name: "/usr/bin/codex")
    client = llm_mod.CodexLLMClient("gpt-6-sol")
    out = client.complete_json("system", "user")
    client.complete("system", "user")
    assert out.data == {"ok": True} and out.usage["input_tokens"] == 16000
    assert (client.input_tokens, client.output_tokens, client.tokens) == (32000, 600, 32600)
    assert (client.cached_input_tokens, client.reasoning_output_tokens) == (18000, 400)
    assert client.log[0]["usage"]["output_tokens"] == 300


def test_results_payload_has_run_fields():
    from clients.jev_agent.report import results_payload

    inv, _ = investigator(lambda i, s, q: dict(origin="deployment/db", origin_p=0.9, enough=0.9))
    res = inv.run()
    rep = build_report(inv.asker, inv.notebook, res, make_ctx())
    llm = FakeLLM([])
    llm.calls, llm.input_tokens, llm.output_tokens = 1, 40, 2
    out = results_payload(res, rep, inv.asker, llm, submission_text=rep.text)
    for key in (
        "origin",
        "fault_category",
        "steps",
        "decision_calls",
        "input_tokens",
        "llm_calls",
        "llm_tokens",
        "escalated",
        "stop_reason",
        "step_trace",
        "submission_text",
    ):
        assert key in out, key
    assert out["decision_calls"] == 4  # 3 steps + evidence selection
    assert out["llm_tokens"] == 42 and out["submission_text"] == rep.text
    json.dumps(out, default=str)


def test_object_candidates_offer_services_configs_and_changed_objects():
    from types import SimpleNamespace

    from clients.jev_agent.notebook import Notebook
    from clients.jev_agent.policy import object_candidates
    from clients.jev_agent.report import affected_neighbors, origin_facts
    from clients.jev_agent.types import AgentContext

    comps = {
        "deployment/search": {
            "kind": "Deployment",
            "name": "search",
            "namespace": "hr",
            "services": [{"name": "search"}, {"name": "frontend"}],
            "config_refs": {"configmaps": ["search-cfg"], "secrets": ["search-sec"]},
        },
        "deployment/frontend": {
            "kind": "Deployment",
            "name": "frontend",
            "namespace": "hr",
            "services": [{"name": "frontend"}],
        },
    }
    overview = {
        "cluster": {"recent_changes": [{"object": "configmap/kube-system/coredns"}, {"object": "deployment/search"}]}
    }
    ctx = AgentContext(app_info={}, namespaces=["hr"], snapshot=SimpleNamespace(components=comps), overview=overview)
    nb = Notebook({"app_name": "hr"}, {"components": []}, scores={})
    nb.actions_done.add("service_view:deployment/search")
    cands = object_candidates(ctx, nb, {"deployment/frontend": 1.0})
    for oid in (
        "service/hr/frontend",
        "service/hr/search",
        "configmap/hr/search-cfg",
        "secret/hr/search-sec",
        "configmap/kube-system/coredns",
    ):
        assert oid in cands
    assert "deployment/search" not in cands  # workloads are offered separately
    facts = origin_facts(ctx, "service/hr/frontend")
    assert facts["kind"] == "Service" and facts["name"] == "frontend" and facts["namespace"] == "hr"
    assert facts["used_by"] == ["deployment/frontend", "deployment/search"]
    assert affected_neighbors(ctx, "service/hr/frontend", {}, None) == ["deployment/frontend", "deployment/search"]


def test_budget_extends_once_after_a_fresh_abnormal_finding():
    from clients.jev_agent.policy import EXTRA_STEPS

    inv, _ = investigator(
        lambda i, s, q: dict(origin="deployment/cart", origin_p=0.4, abnormal=0.9), config=cfg(max_steps=3)
    )
    res = inv.run()
    assert res.stop_reason == "max_steps"
    assert res.tool_steps == 3 + EXTRA_STEPS  # extended exactly once


def test_known_reports_targets_named_in_other_observations():
    nb = make_notebook()
    from clients.jev_agent.types import ToolResult

    nb.add(
        1, "logs", "deployment/cart", ToolResult(lines=["error lines name other components: deployment/valkey-cart"])
    )
    assert "named in O1" in nb.known("deployment/valkey-cart")
    assert "named in" not in nb.known("deployment/cart")


def test_named_targets_rank_first_even_when_healthy():
    nb = make_notebook()
    nb.add(1, "logs", "deployment/frontend", ToolResult(lines=["error lines name other components: deployment/idle"]))
    opts = build_options(make_ctx(), nb, fake_tools(), SCORES, cfg(per_tool=1, max_options=40))
    assert any(k.endswith(":deployment/idle") for k in opts)  # SCORES gives idle 0, yet it is offered


def test_named_targets_come_only_from_structured_links():
    from clients.jev_agent.policy import named_targets

    nb = make_notebook()
    # incidental text (a tail line, an env var name) is not a lead; the logs tool's structured line is
    nb.add(1, "logs", "deployment/frontend", ToolResult(lines=["last lines: idle (10.96.1.2:6379) open"]))
    assert named_targets(make_ctx(), nb) == set()
    nb.add(2, "logs", "deployment/cart", ToolResult(lines=["error lines name other components: deployment/idle"]))
    assert named_targets(make_ctx(), nb) == {"deployment/idle"}
    assert "named in O2" in nb.known("deployment/idle")
    # once examined it no longer jumps the queue
    nb.add(3, "describe", "deployment/idle", ToolResult(lines=["ok"]))
    assert named_targets(make_ctx(), nb) == set()


def test_interaction_origin_offered_when_errors_name_a_callee():
    from types import SimpleNamespace

    from clients.jev_agent.notebook import Notebook
    from clients.jev_agent.policy import object_candidates
    from clients.jev_agent.report import origin_facts
    from clients.jev_agent.types import AgentContext

    comps = {
        "deployment/search": {"kind": "Deployment", "name": "search", "namespace": "hr"},
        "deployment/rate": {"kind": "Deployment", "name": "rate", "namespace": "hr"},
    }
    ctx = AgentContext(app_info={}, namespaces=["hr"], snapshot=SimpleNamespace(components=comps), overview={})
    nb = Notebook({"app_name": "hr"}, {"components": []}, scores={})
    nb.add(1, "logs", "deployment/search", ToolResult(lines=["error lines name other components: deployment/rate"]))
    cands = object_candidates(ctx, nb, {})
    assert "interaction/hr/search--rate" in cands
    facts = origin_facts(ctx, "interaction/hr/search--rate")
    assert facts["components"] == ["deployment/search", "deployment/rate"]


# --------------------------------------------------------------------------- dependency triage and stopping


def test_error_link_pins_both_ends_and_defers_a_confident_stop():
    tools = fake_tools(lines={("logs", "deployment/frontend"): ["error lines name other components: deployment/db"]})

    def script(i, s, q):
        if i == 0:
            return dict(nxt=lambda keys: "logs:deployment/frontend")
        return dict(origin="deployment/db", origin_p=0.9, enough=0.9)

    inv, _ = investigator(script, tools=tools)
    res = inv.run()
    done = inv.notebook.actions_done
    # caller side checked (pinned first after the link appeared), callee side checked before stopping
    assert "describe:deployment/frontend" in done and "logs:deployment/db" in done
    assert any(s.get("stop_deferred") for s in res.steps)
    assert res.stop_reason == "confident_once" and res.origin == "deployment/db"
    _, second = inv.asker._diag._client.calls[1]
    first_keys = list(second[Q_NEXT].criteria)[:2]
    assert all(second[Q_NEXT].criteria[k].startswith("(dependency check)") for k in first_keys)


def test_origin_keys_of_one_entity_are_summed():
    from clients.jev_agent.policy import entity_view, origin_entity

    assert origin_entity("deployment/db") == origin_entity("service/shop/db") == origin_entity("data/shop/db") == "db"
    assert origin_entity("configmap/shop/db") == "configmap/shop/db"
    probs = {"deployment/db": 0.35, "service/shop/db": 0.3, "deployment/cart": 0.3, NOT_YET_KNOWN: 0.05}
    assert entity_view(probs) == ("db", pytest.approx(0.65), "deployment/db")


def test_stable_stop_and_late_category():
    inv, _ = investigator(lambda i, s, q: dict(origin="deployment/db", origin_p=0.5, enough=0.9))
    res = inv.run()
    assert res.stop_reason == "stable" and res.origin == "deployment/db"
    first_questions = inv.asker._diag._client.calls[0][1]
    assert Q_CATEGORY not in first_questions  # asked only once evidence is close to enough
    assert res.fault_category == "config_value"


def test_template_states_propagation_and_drops_classification_line():
    from clients.jev_agent.types import ToolResult

    inv = _inv_result()
    nb = _nb_with(inv.observations)
    nb.add(
        9, "logs", "deployment/checkout", ToolResult(lines=["error lines name other components: deployment/frontend"])
    )
    asker = DecisionAsker(FakeClient(lambda i, s, q: dict(support=0.9)))
    rep = build_report(asker, nb, inv, make_ctx(), overview_mod=fake_overview_mod())
    assert "Propagation: deployment/checkout's ongoing errors name deployment/frontend." in rep.text
    assert "Classification:" not in rep.text


def test_first_look_pins_logs_of_top_components_with_ongoing_errors():
    from types import SimpleNamespace

    from clients.jev_agent.policy import first_look

    overview = {
        "components": {
            "deployment/search": {"replicas": {"desired": 1, "ready": 1}, "errors": {"ongoing_lines": 60}},
            "deployment/consul": {"replicas": {"desired": 1, "ready": 1}},
            "deployment/db": {"replicas": {"desired": 1, "ready": 0}},
        }
    }
    ctx = AgentContext(app_info={}, namespaces=["ns"], snapshot=SimpleNamespace(components={}), overview=overview)
    scores = {"deployment/search": 3.0, "deployment/consul": 2.0, "deployment/db": 1.0}
    avail = {"logs": list(scores)}
    assert first_look(ctx, scores, avail) == ["deployment/search"]  # consul: no current trouble; db: not top 2
    assert first_look(ctx, {**scores, "deployment/db": 2.5}, avail) == ["deployment/search", "deployment/db"]
    assert first_look(ctx, scores, {"logs": ["deployment/consul"]}) == []  # already read


def test_data_origin_only_for_message_consumers():
    from types import SimpleNamespace

    from clients.jev_agent.policy import object_candidates

    comps = {
        "deployment/worker": {"kind": "Deployment", "name": "worker", "namespace": "ns"},
        "deployment/api": {"kind": "Deployment", "name": "api", "namespace": "ns"},
        "deployment/kafka": {
            "kind": "Deployment",
            "name": "kafka",
            "namespace": "ns",
            "containers": [{"image": "apache/kafka:3"}],
        },
        "deployment/db": {"kind": "Deployment", "name": "db", "namespace": "ns", "containers": [{"image": "mongo:4"}]},
    }
    overview = {
        "components": {
            "deployment/worker": {"depends_on": ["deployment/kafka"]},
            "deployment/api": {"depends_on": ["deployment/db"]},
        }
    }
    ctx = AgentContext(app_info={}, namespaces=["ns"], snapshot=SimpleNamespace(components=comps), overview=overview)
    scores = {"deployment/worker": 2.0, "deployment/api": 2.0}
    keys = set(object_candidates(ctx, make_notebook(), scores))
    assert "data/ns/worker" in keys and "data/ns/api" not in keys


def test_origin_findings_name_the_origin_object_verbatim():
    from clients.jev_agent.report import origin_findings
    from clients.jev_agent.types import ToolResult

    nb = make_notebook()
    nb.add(
        1,
        "cluster_objects",
        "quota",
        ToolResult(lines=["ResourceQuota memory-limit-quota: hard memory=1Gi", "other quota x"]),
    )
    long_msg = 'Warning FailedCreate: pods "search-1" is forbidden: failed quota: memory-limit-quota: must specify memory for: search'
    nb.add(2, "events", "namespace", ToolResult(lines=[long_msg]))
    ctx = make_ctx()
    found = origin_findings(ctx, nb, "resourcequota/shop/memory-limit-quota")
    assert found == [
        "O1 cluster_objects quota: ResourceQuota memory-limit-quota: hard memory=1Gi",
        f"O2 events namespace: {long_msg}",
    ]


def test_confident_stop_waits_for_failing_logs_and_login_clients():
    # the top component is failing (ongoing errors) and reports failed logins; the model is confident at once
    tools = fake_tools(
        lines={("logs", "deployment/frontend"): ["ongoing x40: password authentication failed for user shop"]}
    )

    def script(i, s, q):
        if i == 0:
            return dict(nxt=lambda keys: "describe:deployment/idle", origin="deployment/idle", origin_p=0.9, enough=0.9)
        return dict(origin="deployment/idle", origin_p=0.9, enough=0.9)

    inv, _ = investigator(script, tools=tools, config=cfg(min_steps=1))
    inv.ctx.overview = {
        "components": {
            "deployment/frontend": {
                "replicas": {"desired": 1, "ready": 0},
                "errors": {"ongoing_lines": 40},
                "clients": ["deployment/checkout"],
            },
        }
    }
    res = inv.run()
    done = inv.notebook.actions_done
    assert "logs:deployment/frontend" in done  # read before concluding
    assert "config_and_changes:deployment/checkout" in done  # the client of the server rejecting logins
    deferred = [s["stop_deferred"]["pending"] for s in res.steps if s.get("stop_deferred")]
    assert deferred  # the confident stop was held back until those reads were done


def test_confident_stop_waits_for_the_logs_of_a_stalled_consumer():
    stall = (
        "group g topic t partition 0: committed offset 7 unchanged over 15s while log end moved 10->20; "
        "consumer deployment/cart"
    )
    tools = fake_tools(lines={("describe", "deployment/db"): [stall]})

    def script(i, s, q):
        if i == 0:
            return dict(nxt=lambda keys: "describe:deployment/db", origin="deployment/db", origin_p=0.9, enough=0.9)
        return dict(origin="deployment/db", origin_p=0.9, enough=0.9)

    inv, _ = investigator(script, tools=tools, config=cfg(min_steps=1))
    res = inv.run()
    assert "logs:deployment/cart" in inv.notebook.actions_done  # only its errors tell bad input from a wedged consumer
    assert any("logs:deployment/cart" in s["stop_deferred"]["pending"] for s in res.steps if s.get("stop_deferred"))


def test_report_frames_object_origins_like_jev_diag():
    from clients.jev_agent.report import framing, template_text
    from clients.jev_agent.types import ToolResult

    ctx = make_ctx()
    nb = make_notebook()
    nb.add(
        1,
        "object_view",
        "configmap/kube-system/coredns",
        ToolResult(
            lines=[
                "ConfigMap kube-system/coredns Corefile: template ANY ANY cart.shop.svc.cluster.local { rcode NXDOMAIN }"
            ]
        ),
    )
    nb.add(
        2,
        "object_view",
        "networkpolicy/shop/deny-checkout",
        ToolResult(
            lines=['NetworkPolicy shop/deny-checkout: spec {"podSelector": {"matchLabels": {"app": "checkout"}}}']
        ),
    )
    nb.add(3, "object_view", "configmap/shop/frontend", ToolResult(lines=["ConfigMap shop/frontend: data x=1"]))
    # a shared object outside the application, changed to act on one workload: that workload leads
    assert framing(ctx, "configmap/kube-system/coredns", nb) == ("targeted_component", ["deployment/cart"])
    # a policy object: the object leads, acting on the workload it selects
    assert framing(ctx, "networkpolicy/shop/deny-checkout", nb) == ("object_acting_on", ["deployment/checkout"])
    # an object of the application itself, and a workload: unchanged
    assert framing(ctx, "configmap/shop/frontend", nb)[0] is None
    assert framing(ctx, "deployment/cart", nb)[0] is None

    def inv_for(origin):
        return SimpleNamespace(origin=origin, fault_category=None)

    text = template_text(ctx, inv_for("configmap/kube-system/coredns"), [], [], notebook=nb)
    assert text.startswith("Root cause component: Deployment `cart` in namespace `shop`.")
    assert "Fault object: ConfigMap `coredns` in namespace `kube-system`" in text
    text = template_text(ctx, inv_for("networkpolicy/shop/deny-checkout"), [], [], notebook=nb)
    assert text.startswith(
        "Root cause object: NetworkPolicy `deny-checkout` in namespace `shop`, acting on Deployment `checkout`"
    )


def test_login_clients_include_components_with_credentials_changed_first():
    from clients.jev_agent.policy import login_clients

    comps = {
        "deployment/db": {"name": "db", "namespace": NS, "containers": [{"env_names": []}]},
        "deployment/reviews": {
            "name": "reviews",
            "namespace": NS,
            "containers": [{"env_names": ["DB_CONNECTION_STRING"]}],
        },
        "deployment/catalog": {
            "name": "catalog",
            "namespace": NS,
            "containers": [{"env_names": ["DB_CONNECTION_STRING"]}],
            "config_refs": {"secrets": ["catalog-db-conn"]},
        },
        "deployment/web": {"name": "web", "namespace": NS, "containers": [{"env_names": ["PORT"]}]},
    }
    ctx = AgentContext(
        app_info={},
        namespaces=[NS],
        snapshot=SimpleNamespace(components=comps),
        overview={"components": {}, "cluster": {"recent_changes": [{"object": "deployment/catalog"}]}},
    )
    # no dependency edge reaches the database; the credential-carrying components do, the changed one first
    assert login_clients(ctx, "deployment/db") == ["deployment/catalog", "deployment/reviews"]


def test_login_clients_are_found_in_every_namespace_of_the_application():
    from clients.jev_agent.policy import login_clients

    comps = {
        "deployment/db": {"name": "db", "namespace": "data", "containers": [{"env_names": []}]},
        "deployment/shop": {"name": "shop", "namespace": NS, "containers": [{"env_names": ["DB_PASSWORD"]}]},
        "deployment/other": {"name": "other", "namespace": "elsewhere", "containers": [{"env_names": ["DB_PASSWORD"]}]},
    }
    ctx = AgentContext(
        app_info={},
        namespaces=[NS, "data"],
        snapshot=SimpleNamespace(components=comps),
        overview={"components": {}, "cluster": {}},
    )
    # the database lives in its own namespace; its client in the application's other one
    assert login_clients(ctx, "deployment/db") == ["deployment/shop"]


def test_first_look_describes_components_missing_replicas_and_reads_logs_of_erroring_ones():
    from clients.jev_agent.policy import first_look_actions

    overview = {
        "components": {
            "deployment/tracing": {"replicas": {"desired": 2, "ready": 1}},
            "deployment/search": {"replicas": {"desired": 1, "ready": 1}, "errors": {"ongoing_lines": 60}},
        }
    }
    ctx = AgentContext(app_info={}, namespaces=["ns"], snapshot=SimpleNamespace(components={}), overview=overview)
    scores = {"deployment/tracing": 2.0, "deployment/search": 2.0}
    avail = {"logs": list(scores), "describe": list(scores)}
    assert first_look_actions(ctx, scores, avail) == ["describe:deployment/tracing", "logs:deployment/search"]
    assert first_look_actions(ctx, scores, {"logs": list(scores), "describe": []}) == ["logs:deployment/search"]


def test_first_look_describes_a_workload_serving_two_pod_templates_whatever_its_rank():
    from clients.jev_agent.policy import first_look_actions

    overview = {
        "components": {
            "deployment/db": {"replicas": {"desired": 1, "ready": 0}},
            "deployment/api": {"replicas": {"desired": 1, "ready": 1}, "errors": {"ongoing_lines": 5}},
            "deployment/edge": {"replicas": {"desired": 2, "ready": 2, "updated": 1}},
            "deployment/web": {"replicas": {"desired": 2, "ready": 2}},
        }
    }
    ctx = AgentContext(app_info={}, namespaces=["ns"], snapshot=SimpleNamespace(components={}), overview=overview)
    scores = {"deployment/db": 5.0, "deployment/api": 4.0, "deployment/web": 3.0, "deployment/edge": 2.5}
    avail = {"logs": list(scores), "describe": list(scores)}
    assert first_look_actions(ctx, scores, avail) == [
        "describe:deployment/db",
        "logs:deployment/api",
        "describe:deployment/edge",
    ]
    assert "describe:deployment/edge" not in first_look_actions(ctx, scores, {"logs": list(scores), "describe": []})


def test_overview_raises_a_workload_whose_ready_pods_run_an_older_template():
    from clients.jev_agent import overview as ov

    def comp(name, updated):
        return {
            "kind": "Deployment",
            "name": name,
            "namespace": NS,
            "replicas": {"desired": 2, "ready": 2, "updated": updated},
        }

    view = ov._component_view("deployment/edge", comp("edge", 1), {}, {})
    assert view["replicas"]["updated"] == 1
    assert "updated" not in ov._component_view("deployment/web", comp("web", 2), {}, {})["replicas"]
    scores = ov.component_scores({"components": {"deployment/edge": view}})
    assert scores["deployment/edge"] >= ov.W_OLD_TEMPLATE


def test_the_first_looks_run_before_the_model_chooses():
    def script(i, s, q):
        return dict(nxt=lambda keys: "logs:deployment/idle", origin="deployment/frontend", origin_p=0.9, enough=0.9)

    inv, _ = investigator(script, config=cfg(min_steps=1))
    inv.ctx.overview = {"components": {"deployment/frontend": {"replicas": {"desired": 1, "ready": 0}}}}
    inv.run()
    first = inv.notebook.observations[0]
    assert (first.tool, first.target) == ("describe", "deployment/frontend")  # before any decision


def test_the_decision_model_classifies_new_error_lines_and_evidence_uses_its_labels():
    from clients.jev_agent import evidence

    lines = {
        ("logs", "deployment/db"): [
            "ongoing x9: ERROR nextval: reached maximum value of sequence ids_seq",
            "ongoing x3: WARN connection to cache reset",
        ]
    }
    tools = fake_tools(lines=lines)

    def script(i, s, q):
        def label(text):
            return "data" if "sequence" in text else "connection"

        if i == 0:
            return dict(nxt=lambda keys: "logs:deployment/db", origin="deployment/db", origin_p=0.3, enough=0.2)
        return dict(origin="deployment/db", origin_p=0.9, enough=0.9, error=label)

    inv, _ = investigator(script, tools=tools, config=cfg(min_steps=1))
    inv.run()
    nb = inv.notebook
    assert nb.error_labels[lines[("logs", "deployment/db")][0]] == "data"
    assert nb.error_labels[lines[("logs", "deployment/db")][1]] == "connection"
    # the sequence error is the component's own failure because the model said so, not because of a word list
    assert "own_failure_logs" in evidence.candidate_evidence(inv.ctx, nb, "deployment/db")[0]
    # classifying error lines does not overwrite the step's own label
    assert all(str(step["label"]).startswith(("step_", "conclude")) for step in inv.steps)
