"""Tests for the Codex session-JSONL -> ATIF adapter.

Asserts the adapter against a REAL Codex run fixture.
"""

import json
from pathlib import Path

from atif_converter import Trajectory
from atif_converter.adapters import codex

FIXTURE = Path(__file__).parent / "fixtures" / "codex_run"
SESSION_FILE = next((FIXTURE / "sessions").rglob("*.jsonl"))

# Golden expectations for the committed fixture
EXPECTED_AGENT_STEPS = 21
EXPECTED_USER_STEPS = 2
EXPECTED_TOTAL_STEPS = 24
EXPECTED_TOOL_CALLS = 33
EXPECTED_MATCHED_RESULTS = 33
EXPECTED_METRICS_STEPS = 21


def _convert() -> Trajectory:
    return codex.convert_file(SESSION_FILE)


def test_to_atif_returns_validated_trajectory():
    traj = _convert()
    assert isinstance(traj, Trajectory)
    assert traj.schema_version == "ATIF-v1.7"
    assert traj.agent.name == "codex"
    assert traj.agent.version == "0.142.4"
    assert traj.agent.model_name == "gpt-5.4-mini"
    assert traj.session_id == "019f1451-1903-7092-b4ca-73a57d9bdd9d"


def test_steps_are_grouped_by_api_call():
    """Each token_count-delimited API call becomes exactly one agent step."""
    traj = _convert()
    agent_steps = [s for s in traj.steps if s.source == "agent"]
    user_steps = [s for s in traj.steps if s.source == "user"]
    assert len(agent_steps) == EXPECTED_AGENT_STEPS
    assert len(user_steps) == EXPECTED_USER_STEPS
    assert len(traj.steps) == EXPECTED_TOTAL_STEPS


def test_tool_calls_and_results_are_coalesced():
    """Each tool_call and its matching output land in the same step."""
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


def test_per_step_metrics_from_token_count():
    """Each agent step carries metrics from its delimiting token_count event."""
    traj = _convert()
    metrics_steps = [s for s in traj.steps if s.metrics]
    assert len(metrics_steps) == EXPECTED_METRICS_STEPS


def test_final_metrics_aggregate():
    """FinalMetrics come from the last token_count event's total_token_usage."""
    traj = _convert()
    fm = traj.final_metrics
    assert fm is not None
    assert fm.total_prompt_tokens == 613279
    assert fm.total_completion_tokens == 5721
    assert fm.total_cached_tokens == 541312
    assert fm.total_cost_usd is None
    assert fm.total_steps == EXPECTED_TOTAL_STEPS


def test_to_json_dict_is_json_serializable():
    traj = _convert()
    data = traj.to_json_dict()
    serialized = json.dumps(data)
    assert json.loads(serialized) == data
    Trajectory.model_validate(data)


def test_standalone_conversion_has_no_sregym_metadata():
    assert _convert().extra is None
