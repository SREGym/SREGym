"""Golden tests for the Copilot CLI -> ATIF adapter.

Covers both event shapes ``copilot --output-format json`` can emit:

* ``copilot_run_flat``    — flat Anthropic schema (message/tool_use/tool_result/usage)
* ``copilot_run_real``    — real captured run (session-event GPT schema, ground truth)

Session-schema edge behaviors (interleaved parallel tool results, result payload)
are covered in test_copilot_edge_cases.py.

Fixtures are synthetic (built from Harbor's documented event shapes) until a real
``copilot-cli.jsonl`` run is captured; see PLAN_copilot.md step 10.
"""

import json
from pathlib import Path

from sregym.traces.adapters import copilot
from sregym.traces.atif import Trajectory

FIXTURES = Path(__file__).parent / "fixtures"
FLAT = FIXTURES / "copilot_run_flat"
# Real captured run (post client-fix): session-event schema, claude-sonnet-4-6.
REAL = FIXTURES / "copilot_run_real"


def _convert(fixture: Path) -> Trajectory:
    return copilot.to_atif(fixture, sregym_meta={"problem_id": "p", "run": 1})


# --------------------------------------------------------------------------- #
# flat (Anthropic) schema
# --------------------------------------------------------------------------- #
def test_flat_returns_validated_trajectory():
    t = _convert(FLAT)
    assert isinstance(t, Trajectory)
    assert t.schema_version == "ATIF-v1.7"
    assert t.agent.name == "copilot"
    assert t.agent.model_name == "claude-sonnet-4-6"
    assert t.session_id == "copilot-cli"


def test_flat_step_shape():
    t = _convert(FLAT)
    sources = [s.source for s in t.steps]
    assert sources == ["user", "agent", "agent", "agent", "agent"]
    assert t.steps[0].message.startswith("Diagnose")


def test_flat_tool_calls_and_matched_results():
    t = _convert(FLAT)
    tool_steps = [s for s in t.steps if s.tool_calls]
    assert len(tool_steps) == 2
    # Each tool_use step got its tool_result attached by id.
    first = tool_steps[0]
    assert first.tool_calls[0].function_name == "bash"
    assert first.tool_calls[0].tool_call_id == "toolu_1"
    assert first.observation is not None
    assert first.observation.results[0].source_call_id == "toolu_1"
    assert "Pending" in first.observation.results[0].content


def test_flat_tool_result_error_flag_preserved():
    t = _convert(FLAT)
    first_tool = next(s for s in t.steps if s.tool_calls)
    # is_error rides along under the observation result's extra.
    assert first_tool.observation.results[0].extra == {"is_error": False}


def test_flat_usage_summed():
    t = _convert(FLAT)
    fm = t.final_metrics
    assert fm.total_prompt_tokens == 1200 + 1500 + 1600
    assert fm.total_completion_tokens == 40 + 30 + 25
    assert fm.total_steps == 5


def test_flat_roundtrips():
    t = _convert(FLAT)
    Trajectory.model_validate(json.loads(json.dumps(t.to_json_dict())))


# --------------------------------------------------------------------------- #
# REAL captured run (results/0704_2011) — ground-truth session-event schema
# --------------------------------------------------------------------------- #
def test_real_run_shape():
    t = _convert(REAL)
    assert isinstance(t, Trajectory)
    assert t.schema_version == "ATIF-v1.7"
    assert t.agent.name == "copilot"
    assert t.agent.model_name == "claude-sonnet-4-6"
    assert len(t.steps) == 9
    assert [s.source for s in t.steps].count("user") == 1
    assert t.steps[0].source == "user"


def test_real_run_reasoning_captured():
    """Copilot's ``reasoningText`` maps to first-class ``reasoning_content``."""
    t = _convert(REAL)
    reasoning_steps = [s for s in t.steps if s.reasoning_content]
    assert len(reasoning_steps) == 4
    assert "hotel reservation" in reasoning_steps[0].reasoning_content.lower()
    # The redundant standalone assistant.reasoning events must NOT double it.
    assert len(reasoning_steps) == 4


def test_real_run_tool_calls_all_matched():
    t = _convert(REAL)
    tool_calls = sum(len(s.tool_calls or []) for s in t.steps)
    observations = sum(len(s.observation.results) for s in t.steps if s.observation)
    assert tool_calls == 10
    assert observations == 10


def test_real_run_metrics_and_result():
    t = _convert(REAL)
    fm = t.final_metrics
    assert fm.total_completion_tokens == 1642
    assert fm.total_prompt_tokens is None  # session schema reports no input tokens
    assert fm.total_steps == 9
    assert fm.extra is not None and "copilot_result" in fm.extra
    assert fm.extra["copilot_result"]["exitCode"] == 0


def test_real_run_roundtrips():
    t = _convert(REAL)
    Trajectory.model_validate(json.loads(json.dumps(t.to_json_dict())))


# --------------------------------------------------------------------------- #
# extra.sregym injection
# --------------------------------------------------------------------------- #
def test_sregym_meta_injected():
    t = _convert(FLAT)
    assert t.extra["sregym"] == {"problem_id": "p", "run": 1}
