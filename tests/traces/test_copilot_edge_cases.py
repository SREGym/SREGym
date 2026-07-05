"""Edge-case coverage for the Copilot CLI -> ATIF adapter."""

import json
from pathlib import Path

from sregym.traces.adapters import copilot
from sregym.traces.atif import Trajectory


def _run_dir(tmp_path: Path) -> Path:
    d = tmp_path / "results" / "b" / "copilot" / "p_hotel_reservation" / "run_1"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_jsonl(run_dir: Path, lines: list) -> None:
    """Write mixed content (dicts -> JSON lines, str -> raw lines) to copilot-cli.jsonl."""
    out = []
    for item in lines:
        out.append(item if isinstance(item, str) else json.dumps(item))
    (run_dir / "copilot-cli.jsonl").write_text("\n".join(out) + "\n")


def test_no_jsonl_file_returns_none(tmp_path):
    assert copilot.to_atif(_run_dir(tmp_path), sregym_meta={"problem_id": "p", "run": 1}) is None


def test_empty_file_returns_none(tmp_path):
    run_dir = _run_dir(tmp_path)
    (run_dir / "copilot-cli.jsonl").write_text("")
    assert copilot.to_atif(run_dir) is None


def test_only_non_json_lines_returns_none(tmp_path):
    run_dir = _run_dir(tmp_path)
    _write_jsonl(run_dir, ["booting...", "some stderr noise", "not json at all"])
    assert copilot.to_atif(run_dir) is None


def test_non_json_lines_are_skipped_not_fatal(tmp_path):
    run_dir = _run_dir(tmp_path)
    _write_jsonl(
        run_dir,
        [
            "stderr: starting copilot",
            {"type": "message", "role": "user", "content": "hi"},
            "stderr: some warning",
            {"type": "message", "role": "assistant", "content": "hello", "model": "m"},
        ],
    )
    t = copilot.to_atif(run_dir)
    assert t is not None
    assert [s.source for s in t.steps] == ["user", "agent"]


def test_auth_error_file_returns_none(tmp_path):
    run_dir = _run_dir(tmp_path)
    (run_dir / "copilot-cli.jsonl").write_text("Error: No authentication information found\nplease log in\n")
    assert copilot.to_atif(run_dir) is None


def test_malformed_event_is_salvaged_not_dropped(tmp_path):
    run_dir = _run_dir(tmp_path)
    # assistant.message with no `data` key raises KeyError inside the handler.
    _write_jsonl(
        run_dir,
        [
            {"type": "message", "role": "user", "content": "hi"},
            {"type": "assistant.message"},  # missing "data" -> salvaged
            {"type": "message", "role": "assistant", "content": "recovered"},
        ],
    )
    t = copilot.to_atif(run_dir)
    assert t is not None
    salvaged = [s for s in t.steps if s.extra and "copilot_parse_error" in s.extra]
    assert len(salvaged) == 1
    assert salvaged[0].extra["raw_event"]["type"] == "assistant.message"
    # The run still parses the surrounding events.
    assert any(s.message == "recovered" for s in t.steps)


def test_orphan_tool_result_kept_as_own_step(tmp_path):
    run_dir = _run_dir(tmp_path)
    _write_jsonl(
        run_dir,
        [
            {"type": "message", "role": "user", "content": "go"},
            # tool_result with no preceding tool_use for its id.
            {"type": "tool_result", "tool_use_id": "orphan", "content": "leftover output"},
        ],
    )
    t = copilot.to_atif(run_dir)
    assert t is not None
    orphan = next(s for s in t.steps if s.extra and s.extra.get("source_call_id") == "orphan")
    assert "leftover output" in orphan.message


def test_unknown_event_types_do_not_break(tmp_path):
    run_dir = _run_dir(tmp_path)
    _write_jsonl(
        run_dir,
        [
            {"type": "session.started", "data": {}},
            {"type": "assistant.turn_start", "data": {}},
            {"type": "message", "role": "user", "content": "hi"},
        ],
    )
    t = copilot.to_atif(run_dir)
    assert t is not None
    # Only the user message becomes a step; lifecycle events are skipped.
    assert len(t.steps) == 1
    assert t.steps[0].source == "user"


def test_non_object_line_skipped(tmp_path):
    run_dir = _run_dir(tmp_path)
    _write_jsonl(
        run_dir,
        [
            [1, 2, 3],  # valid JSON, but not an event object
            {"type": "message", "role": "user", "content": "hi"},
        ],
    )
    t = copilot.to_atif(run_dir)
    assert t is not None
    assert len(t.steps) == 1


def test_tool_use_string_arguments_normalized(tmp_path):
    run_dir = _run_dir(tmp_path)
    _write_jsonl(
        run_dir,
        [
            {"type": "message", "role": "user", "content": "patch it"},
            {"type": "tool_use", "id": "t1", "name": "apply_patch", "input": "*** raw patch text ***"},
        ],
    )
    t = copilot.to_atif(run_dir)
    tc = next(s for s in t.steps if s.tool_calls).tool_calls[0]
    assert tc.arguments == {"value": "*** raw patch text ***"}


def test_sregym_meta_absent_leaves_extra_unset(tmp_path):
    run_dir = _run_dir(tmp_path)
    _write_jsonl(run_dir, [{"type": "message", "role": "user", "content": "hi"}])
    t = copilot.to_atif(run_dir)  # no sregym_meta
    assert t is not None
    assert t.extra is None


def test_roundtrip_after_salvage(tmp_path):
    run_dir = _run_dir(tmp_path)
    _write_jsonl(
        run_dir,
        [
            {"type": "message", "role": "user", "content": "hi"},
            {"type": "assistant.message"},  # salvaged
        ],
    )
    t = copilot.to_atif(run_dir)
    Trajectory.model_validate(json.loads(json.dumps(t.to_json_dict())))


def test_interleaved_parallel_tool_results_matched_by_id(tmp_path):
    """Session schema: parallel calls whose results arrive out of order.

    An assistant.message issues call_1 and call_2; their tool.execution_complete
    events arrive reversed (call_2 first). Both must attach to the issuing step
    by id, not position.
    """
    run_dir = _run_dir(tmp_path)
    _write_jsonl(
        run_dir,
        [
            {"type": "user.message", "data": {"content": "go"}},
            {
                "type": "assistant.message",
                "data": {
                    "content": "checking",
                    "model": "gpt-5.2",
                    "outputTokens": 10,
                    "toolRequests": [
                        {"toolCallId": "call_1", "name": "bash", "arguments": {"command": "a"}},
                        {"toolCallId": "call_2", "name": "bash", "arguments": {"command": "b"}},
                    ],
                },
            },
            {"type": "tool.execution_complete", "data": {"toolCallId": "call_2", "result": {"stdout": "out-2"}}},
            {"type": "tool.execution_complete", "data": {"toolCallId": "call_1", "result": {"stdout": "out-1"}}},
        ],
    )
    t = copilot.to_atif(run_dir)
    agent_step = next(s for s in t.steps if s.tool_calls)
    assert len(agent_step.tool_calls) == 2
    results = {r.source_call_id: r.content for r in agent_step.observation.results}
    assert set(results) == {"call_1", "call_2"}
    assert "out-1" in results["call_1"]
    assert "out-2" in results["call_2"]


def test_result_payload_preserved_on_final_metrics(tmp_path):
    run_dir = _run_dir(tmp_path)
    _write_jsonl(
        run_dir,
        [
            {"type": "user.message", "data": {"content": "go"}},
            {"type": "assistant.message", "data": {"content": "done", "model": "m", "outputTokens": 3}},
            {"type": "result", "exitCode": 0, "usage": {"totalTokens": 100}},
        ],
    )
    t = copilot.to_atif(run_dir)
    assert t.final_metrics.extra["copilot_result"]["exitCode"] == 0
