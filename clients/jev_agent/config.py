"""Run configuration for jev_agent, read from the environment at call time (so tests can monkeypatch it)."""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass

AGENT_NAME = "jev_agent"

POLICY_MODEL = "model"
POLICY_SCRIPTED = "scripted"

# Variables forwarded into the agent container as they are (see sregym/service/container_runner.py).
ENV_VARS = (
    "JEV_AGENT_MAX_STEPS",
    "JEV_AGENT_MIN_STEPS",
    "JEV_AGENT_WALL_BUDGET_S",
    "JEV_AGENT_POLICY",
    "JEV_AGENT_LLM_MODEL",
    "JEV_AGENT_LLM_BACKEND",
    "JEV_AGENT_LLM_EFFORT",
    "JEV_AGENT_LLM_URL",
    "JEV_AGENT_MAX_LLM_CALLS",
    "JEV_AGENT_MAX_OPTIONS",
    "JEV_AGENT_PER_TOOL",
    "JEV_AGENT_STATE_BUDGET",
    "JEV_AGENT_START_DELAY",
    "JEV_AGENT_CRAFT",
    "JEV_AGENT_MAX_CRAFT",
    "JEV_AGENT_ESCALATE",
    "JEV_AGENT_REVIEW",
    "JEV_AGENT_CHANGE_WINDOW_S",
    "JEV_AGENT_ENOUGH_P",
    "JEV_AGENT_ORIGIN_P",
    "JEV_AGENT_ORIGIN_P_ONCE",
    "JEV_AGENT_SUBMIT_P",
    "JEV_AGENT_STABLE_ENOUGH_P",
    "JEV_AGENT_STABLE_ORIGIN_P",
    "JEV_AGENT_STABLE_STEPS",
)
# Read by the agent but not forwarded as they are: the cache directory is mounted at a fixed container path.
NOT_FORWARDED = ("JEV_AGENT_CACHE_DIR",)


def _int(name: str, default: int) -> int:
    """The integer in environment variable `name`, or `default` when it is unset or not an integer."""
    raw = os.environ.get(name, "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    """The number in environment variable `name`, or `default` when it is unset or not a number."""
    raw = os.environ.get(name, "").strip()
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


@dataclass
class AgentConfig:
    """Run settings of one diagnosis: step and time budgets, policy, LLM use and Decisions model."""

    max_steps: int = 30  # tool steps (evidence-based stopping usually ends far earlier)
    min_steps: int = 2  # never stop before this many tool steps
    wall_budget_s: float = 300.0  # investigation wall-clock budget: the main safety valve
    policy: str = POLICY_MODEL  # model | scripted
    llm_model: str = ""  # "" = no generative LLM (no escalation; template report)
    max_llm_calls: int = 2
    max_options: int = 40
    per_tool: int = 4
    state_budget: int = 24000  # tokens for the notebook state sent with each decision request
    start_delay: float = 120.0
    overview_budget: int = 4000
    craft: bool = False  # LLM-crafted read-only tools: explore before the investigation + a CRAFT menu option
    # CRAFT choices per investigation (each one LLM call, plus one retry when rejected). Off by default: on the
    # recorded runs the tools written mid-investigation rarely changed the answer and cost 30-90 s per run.
    max_craft: int = 0
    escalate: bool = True  # one LLM call for hypotheses when the investigation stalls (escalate.trigger)
    review: bool = True  # one LLM call that reviews the origin before the report (driver._review_origin)
    cache_dir: str = ""  # explore results per environment (empty: no cache)
    change_window_s: float = 1800.0  # writes this long before the symptoms began are history, not changes
    # Stop rules (policy._should_stop): enough_evidence >= enough_p with the same origin >= origin_p on two
    # consecutive steps or >= origin_p_once once; submit_now chosen with p >= submit_p; or stable_steps steps in
    # a row with enough_evidence >= stable_enough_p and the same origin >= stable_origin_p.
    enough_p: float = 0.7
    origin_p: float = 0.6
    origin_p_once: float = 0.85
    submit_p: float = 0.5
    stable_enough_p: float = 0.85
    stable_origin_p: float = 0.4
    stable_steps: int = 3

    def to_dict(self) -> dict:
        """The settings as a plain dict (for the trace)."""
        return asdict(self)


def load() -> AgentConfig:
    """Settings from the JEV_AGENT_* environment variables, with the defaults above."""
    policy = os.environ.get("JEV_AGENT_POLICY", POLICY_MODEL).strip().lower() or POLICY_MODEL
    if policy not in (POLICY_MODEL, POLICY_SCRIPTED):
        raise ValueError(f"JEV_AGENT_POLICY must be '{POLICY_MODEL}' or '{POLICY_SCRIPTED}', got {policy!r}")
    return AgentConfig(
        max_steps=max(1, _int("JEV_AGENT_MAX_STEPS", 30)),
        min_steps=max(0, _int("JEV_AGENT_MIN_STEPS", 2)),
        wall_budget_s=_float("JEV_AGENT_WALL_BUDGET_S", 300.0),
        policy=policy,
        llm_model=os.environ.get("JEV_AGENT_LLM_MODEL", "").strip(),
        max_llm_calls=max(0, _int("JEV_AGENT_MAX_LLM_CALLS", 2)),
        max_options=max(1, _int("JEV_AGENT_MAX_OPTIONS", 40)),
        per_tool=max(1, _int("JEV_AGENT_PER_TOOL", 4)),
        state_budget=max(2000, _int("JEV_AGENT_STATE_BUDGET", 24000)),
        start_delay=max(0.0, _float("JEV_AGENT_START_DELAY", 120.0)),
        craft=os.environ.get("JEV_AGENT_CRAFT", "").strip().lower() in ("1", "true", "yes", "on"),
        max_craft=max(0, _int("JEV_AGENT_MAX_CRAFT", 0)),
        escalate=os.environ.get("JEV_AGENT_ESCALATE", "").strip().lower() not in ("0", "false", "no", "off"),
        review=os.environ.get("JEV_AGENT_REVIEW", "").strip().lower() not in ("0", "false", "no", "off"),
        cache_dir=os.environ.get("JEV_AGENT_CACHE_DIR", "").strip(),
        change_window_s=max(60.0, _float("JEV_AGENT_CHANGE_WINDOW_S", 1800.0)),
        enough_p=_float("JEV_AGENT_ENOUGH_P", 0.7),
        origin_p=_float("JEV_AGENT_ORIGIN_P", 0.6),
        origin_p_once=_float("JEV_AGENT_ORIGIN_P_ONCE", 0.85),
        submit_p=_float("JEV_AGENT_SUBMIT_P", 0.5),
        stable_enough_p=_float("JEV_AGENT_STABLE_ENOUGH_P", 0.85),
        stable_origin_p=_float("JEV_AGENT_STABLE_ORIGIN_P", 0.4),
        stable_steps=max(2, _int("JEV_AGENT_STABLE_STEPS", 3)),
    )
