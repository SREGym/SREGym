"""Tests for the Claude Code -> ATIF adapter.

Asserts the adapter against a REAL Claude Code run fixture.
"""

import json
from pathlib import Path

from atif_converter import Trajectory
from atif_converter.adapters import claudecode

FIXTURE = Path(__file__).parent / "fixtures" / "claudecode_run"

# Golden expectations for the committed fixture
EXPECTED_AGENT_STEPS = 13  # distinct assistant message.ids (31 raw events coalesce)
EXPECTED_USER_STEPS = 1
EXPECTED_TOTAL_STEPS = 14
EXPECTED_MATCHED_RESULTS = 16  # tool_result blocks matched to their tool_call
EXPECTED_TOTAL_COST_USD = 0.2282426


def _convert() -> Trajectory:
    session_files = list((FIXTURE / "sessions" / "projects" / "-logs").glob("*.jsonl"))
    return claudecode.convert_files(session_files, total_cost_usd=EXPECTED_TOTAL_COST_USD)


def test_to_atif_returns_validated_trajectory():
    traj = _convert()
    assert isinstance(traj, Trajectory)
    assert traj.schema_version == "ATIF-v1.7"
    assert traj.agent.name == "claudecode"
    assert traj.agent.version == "2.1.195"
    assert traj.agent.model_name == "claude-sonnet-4-6"
    assert traj.session_id == "74bfdd52-f1bd-477d-98c8-306bde810080"


def test_steps_are_coalesced_by_message_id():
    traj = _convert()
    agent_steps = [s for s in traj.steps if s.source == "agent"]
    user_steps = [s for s in traj.steps if s.source == "user"]

    # 13 distinct assistant message.ids => 13 agent steps (NOT 31).
    assert len(agent_steps) == EXPECTED_AGENT_STEPS, (
        f"expected {EXPECTED_AGENT_STEPS} coalesced agent steps, got {len(agent_steps)}"
    )
    # One real user prompt; the 16 tool_result user records attach to agent
    # steps and do not create separate steps.
    assert len(user_steps) == EXPECTED_USER_STEPS
    assert len(traj.steps) == EXPECTED_TOTAL_STEPS


def test_tool_results_attach_to_correct_tool_call():
    traj = _convert()
    # Every observation.source_call_id must reference a tool_call_id in the same
    # step (the model validator already enforces this; assert we actually have
    # matched results, not just an empty set).
    matched = 0
    for step in traj.steps:
        if not step.observation:
            continue
        call_ids = {tc.tool_call_id for tc in (step.tool_calls or [])}
        for result in step.observation.results:
            assert result.source_call_id in call_ids
            matched += 1
    # Every tool_result in the fixture is matched to its originating tool_call;
    # a regression that drops results would lower this count.
    assert matched == EXPECTED_MATCHED_RESULTS


def test_total_cost_from_stream_json():
    traj = _convert()
    assert traj.final_metrics is not None
    assert traj.final_metrics.total_cost_usd == EXPECTED_TOTAL_COST_USD


def test_token_totals_not_double_counted():
    traj = _convert()
    fm = traj.final_metrics
    # Sum of per-step completion tokens equals the FinalMetrics aggregate, and
    # each message.id contributes its usage exactly once (no inflation from the
    # 31 raw events).
    per_step_completion = sum(
        s.metrics.completion_tokens for s in traj.steps if s.metrics and s.metrics.completion_tokens is not None
    )
    assert fm.total_completion_tokens == per_step_completion
    # Exactly the coalesced agent steps carry metrics (one per message.id);
    # an inflation regression would raise this above EXPECTED_AGENT_STEPS.
    steps_with_metrics = [s for s in traj.steps if s.metrics is not None]
    assert len(steps_with_metrics) == EXPECTED_AGENT_STEPS


def test_to_json_dict_is_json_serializable():
    traj = _convert()
    payload = traj.to_json_dict()
    # Must not raise: agent.extra set-valued fields (cwds/git_branches/agent_ids)
    # are coerced to sorted lists by the adapter.
    text = json.dumps(payload)
    assert text
    # Re-validate round-trip.
    Trajectory.model_validate(payload)
