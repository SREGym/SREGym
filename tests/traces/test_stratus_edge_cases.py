"""Edge-case coverage for the Stratus -> ATIF adapter."""

import json
from pathlib import Path

from sregym.traces.adapters import stratus
from sregym.traces.atif import Trajectory

TRAJ_NAME = "0705_0000_p_stratus_agent_trajectory.jsonl"


def _run_dir(tmp_path: Path) -> Path:
    d = tmp_path / "results" / "b" / "stratus" / "p_hotel_reservation" / "run_1"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write(run_dir: Path, lines: list) -> None:
    out = []
    for item in lines:
        out.append(item if isinstance(item, str) else json.dumps(item))
    (run_dir / TRAJ_NAME).write_text("\n".join(out) + "\n")


def _event(stage, idx, messages, *, submitted=False, num_steps=0):
    return {
        "type": "event",
        "stage": stage,
        "event_index": idx,
        "num_steps": num_steps,
        "submitted": submitted,
        "rollback_stack": "",
        "messages": messages,
        "last_message": messages[-1] if messages else {},
    }


def test_no_trajectory_file_returns_none(tmp_path):
    assert stratus.to_atif(_run_dir(tmp_path), sregym_meta={"problem_id": "p", "run": 1}) is None


def test_empty_file_returns_none(tmp_path):
    run_dir = _run_dir(tmp_path)
    (run_dir / TRAJ_NAME).write_text("")
    assert stratus.to_atif(run_dir) is None


def test_metadata_only_returns_none(tmp_path):
    run_dir = _run_dir(tmp_path)
    _write(run_dir, [{"type": "metadata", "problem_id": "p", "total_stages": 0}])
    assert stratus.to_atif(run_dir) is None


def test_malformed_line_skipped(tmp_path):
    run_dir = _run_dir(tmp_path)
    _write(
        run_dir,
        [
            {"type": "metadata", "problem_id": "p"},
            "this is not json { broken",
            _event("diagnosis", 0, [{"type": "HumanMessage", "content": "hi"}]),
        ],
    )
    t = stratus.to_atif(run_dir)
    assert t is not None
    assert len(t.steps) == 1


def test_uses_last_event_per_stage(tmp_path):
    """Cumulative snapshots: only the highest-event_index event of a stage is used."""
    run_dir = _run_dir(tmp_path)
    m0 = [{"type": "HumanMessage", "content": "start"}]
    m1 = m0 + [{"type": "AIMessage", "content": "step 1"}]
    m2 = m1 + [{"type": "AIMessage", "content": "step 2"}]
    _write(
        run_dir,
        [
            {"type": "metadata", "problem_id": "p"},
            _event("diagnosis", 0, m0),
            _event("diagnosis", 1, m1),
            _event("diagnosis", 2, m2),  # full snapshot
        ],
    )
    t = stratus.to_atif(run_dir)
    # 1 user + 2 agent from the last snapshot (not 1+1+2 from all snapshots).
    assert len(t.steps) == 3
    assert [s.source for s in t.steps] == ["user", "agent", "agent"]


def test_events_out_of_order_still_picks_max_index(tmp_path):
    run_dir = _run_dir(tmp_path)
    full = [
        {"type": "HumanMessage", "content": "hi"},
        {"type": "AIMessage", "content": "a"},
        {"type": "AIMessage", "content": "b"},
    ]
    _write(
        run_dir,
        [
            {"type": "metadata", "problem_id": "p"},
            _event("diagnosis", 1, full),  # highest index first in file
            _event("diagnosis", 0, [{"type": "HumanMessage", "content": "hi"}]),
        ],
    )
    t = stratus.to_atif(run_dir)
    assert len(t.steps) == 3


def test_parallel_calls_positional_fallback(tmp_path):
    """No tool_call_id -> N ToolMessages map to the AIMessage's N calls in order."""
    run_dir = _run_dir(tmp_path)
    msgs = [
        {"type": "HumanMessage", "content": "go"},
        {
            "type": "AIMessage",
            "content": "",
            "tool_calls": [
                {"name": "t1", "args": {}, "id": "c1"},
                {"name": "t2", "args": {}, "id": "c2"},
            ],
        },
        {"type": "ToolMessage", "content": "out-1"},  # no tool_call_id
        {"type": "ToolMessage", "content": "out-2"},
    ]
    _write(run_dir, [{"type": "metadata", "problem_id": "p"}, _event("diagnosis", 0, msgs)])
    t = stratus.to_atif(run_dir)
    agent = next(s for s in t.steps if s.tool_calls)
    results = {r.source_call_id: r.content for r in agent.observation.results}
    assert results == {"c1": "out-1", "c2": "out-2"}


def test_orphan_tool_message_kept_as_step(tmp_path):
    run_dir = _run_dir(tmp_path)
    msgs = [
        {"type": "HumanMessage", "content": "go"},
        {"type": "ToolMessage", "content": "orphan output"},  # no preceding call
    ]
    _write(run_dir, [{"type": "metadata", "problem_id": "p"}, _event("diagnosis", 0, msgs)])
    t = stratus.to_atif(run_dir)
    assert any("orphan output" in (s.message or "") for s in t.steps)


def test_string_tool_args_normalized(tmp_path):
    run_dir = _run_dir(tmp_path)
    msgs = [
        {"type": "HumanMessage", "content": "patch"},
        {
            "type": "AIMessage",
            "content": "",
            "tool_calls": [{"name": "apply_patch", "args": "*** raw ***", "id": "c1"}],
        },
    ]
    _write(run_dir, [{"type": "metadata", "problem_id": "p"}, _event("diagnosis", 0, msgs)])
    t = stratus.to_atif(run_dir)
    tc = next(s for s in t.steps if s.tool_calls).tool_calls[0]
    assert tc.arguments == {"value": "*** raw ***"}


def test_multi_stage_submitted_any(tmp_path):
    run_dir = _run_dir(tmp_path)
    _write(
        run_dir,
        [
            {"type": "metadata", "problem_id": "p"},
            _event("diagnosis", 0, [{"type": "AIMessage", "content": "d"}], submitted=False),
            _event("mitigation_attempt_0", 0, [{"type": "AIMessage", "content": "m"}], submitted=True),
        ],
    )
    t = stratus.to_atif(run_dir)
    assert t.extra["sregym"]["submitted"] is True


def test_sregym_meta_absent_still_has_stages(tmp_path):
    run_dir = _run_dir(tmp_path)
    _write(
        run_dir,
        [{"type": "metadata", "problem_id": "p"}, _event("diagnosis", 0, [{"type": "AIMessage", "content": "d"}])],
    )
    t = stratus.to_atif(run_dir)  # no sregym_meta
    assert t is not None
    assert "stages" in t.extra["sregym"]


def test_roundtrip_multi_stage(tmp_path):
    run_dir = _run_dir(tmp_path)
    _write(
        run_dir,
        [
            {"type": "metadata", "problem_id": "p"},
            _event("diagnosis", 0, [{"type": "HumanMessage", "content": "hi"}, {"type": "AIMessage", "content": "d"}]),
            _event("mitigation_attempt_0", 0, [{"type": "AIMessage", "content": "m"}]),
        ],
    )
    t = stratus.to_atif(run_dir)
    Trajectory.model_validate(json.loads(json.dumps(t.to_json_dict())))
