"""Tests for the OpenCode -> ATIF adapter.

Uses a REAL OpenCode run fixture. Primary source is the exported session JSON
(``sessions/.../session-*.json``); ``opencode.txt`` is kept as a fallback.
"""

import json
from pathlib import Path

from atif_converter import Trajectory
from atif_converter.adapters import opencode

FIXTURE = Path(__file__).parent / "fixtures" / "opencode_run"
SESSION_FILE = next((FIXTURE / "sessions").rglob("session-*.json"))

EXPECTED_AGENT_STEPS = 13
EXPECTED_USER_STEPS = 1
EXPECTED_TOTAL_STEPS = 14
EXPECTED_TOOL_CALLS = 18
EXPECTED_MATCHED_RESULTS = 18
EXPECTED_METRICS_STEPS = 13
EXPECTED_REASONING_STEPS = 12


def _convert() -> Trajectory:
    return opencode.convert_file(SESSION_FILE)


def test_to_atif_returns_validated_trajectory():
    traj = _convert()
    assert isinstance(traj, Trajectory)
    assert traj.schema_version == "ATIF-v1.7"
    assert traj.agent.name == "opencode"
    assert traj.agent.version == "1.17.11"
    assert traj.agent.model_name == "glm-5.2"
    assert traj.session_id == "ses_0e8fabe63ffe91KaErs7Mh9g5O"


def test_user_step_from_session_json():
    """The session JSON provides the user turn the stream omits."""
    traj = _convert()
    user_steps = [s for s in traj.steps if s.source == "user"]
    assert len(user_steps) == EXPECTED_USER_STEPS
    assert traj.steps[0].source == "user"


def test_steps_are_grouped_by_message_boundaries():
    """Each assistant message becomes one agent step."""
    traj = _convert()
    agent_steps = [s for s in traj.steps if s.source == "agent"]
    assert len(agent_steps) == EXPECTED_AGENT_STEPS
    assert len(traj.steps) == EXPECTED_TOTAL_STEPS


def test_tool_calls_and_results_are_coalesced():
    """Each observation result references a tool_call_id in the same step."""
    traj = _convert()
    total_tool_calls = sum(len(s.tool_calls or []) for s in traj.steps)
    assert total_tool_calls == EXPECTED_TOOL_CALLS

    matched = 0
    for step in traj.steps:
        if not step.observation:
            continue
        call_ids = {tc.tool_call_id for tc in (step.tool_calls or [])}
        for result in step.observation.results:
            assert result.source_call_id in call_ids
            matched += 1
    assert matched == EXPECTED_MATCHED_RESULTS


def test_per_step_metrics_from_step_finish():
    """Each agent step carries metrics from its step-finish part."""
    traj = _convert()
    metrics_steps = [s for s in traj.steps if s.metrics]
    assert len(metrics_steps) == EXPECTED_METRICS_STEPS


def test_reasoning_content_captured():
    """Reasoning parts are extracted into reasoning_content."""
    traj = _convert()
    reasoning_steps = [s for s in traj.steps if s.reasoning_content]
    assert len(reasoning_steps) == EXPECTED_REASONING_STEPS


def test_final_metrics_from_session_info():
    """FinalMetrics use the session JSON's authoritative info.tokens."""
    traj = _convert()
    fm = traj.final_metrics
    assert fm is not None
    assert fm.total_prompt_tokens == 232529  # input + cache_read (ATIF convention)
    assert fm.total_completion_tokens == 1963
    assert fm.total_cached_tokens == 216704
    assert fm.total_cost_usd is None  # opencode reports cost 0
    assert fm.total_steps == EXPECTED_TOTAL_STEPS
    # Raw input preserved in extra (matches results JSON usage_metrics).
    assert fm.extra["input_tokens"] == 15825
    assert fm.extra["reasoning_tokens"] == 2388


def test_to_json_dict_is_json_serializable():
    traj = _convert()
    data = traj.to_json_dict()
    serialized = json.dumps(data)
    assert json.loads(serialized) == data
    Trajectory.model_validate(data)


def test_standalone_conversion_has_no_sregym_metadata():
    assert _convert().extra is None
