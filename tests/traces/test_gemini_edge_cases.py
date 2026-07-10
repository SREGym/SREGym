"""Edge-case coverage for the Gemini CLI -> ATIF adapter."""

import json
from pathlib import Path

from sregym.traces.adapters import gemini
from sregym.traces.atif import Trajectory


def _run_dir(tmp_path: Path) -> Path:
    d = tmp_path / "results" / "b" / "gemini" / "p_hotel_reservation" / "run_1"
    (d / "sessions" / "2026" / "07" / "05").mkdir(parents=True, exist_ok=True)
    return d


def _write_session(run_dir: Path, obj_or_lines) -> None:
    p = run_dir / "sessions" / "2026" / "07" / "05" / "session-x.json"
    if isinstance(obj_or_lines, list):
        p.write_text("\n".join(json.dumps(x) for x in obj_or_lines) + "\n")
    else:
        p.write_text(json.dumps(obj_or_lines))


def test_no_sessions_dir_returns_none(tmp_path):
    d = tmp_path / "results" / "b" / "gemini" / "p" / "run_1"
    d.mkdir(parents=True)
    assert gemini.to_atif(d, sregym_meta={"problem_id": "p", "run": 1}) is None


def test_empty_session_returns_none(tmp_path):
    run_dir = _run_dir(tmp_path)
    _write_session(run_dir, {"sessionId": "x", "messages": []})
    assert gemini.to_atif(run_dir) is None


def test_empty_file_returns_none(tmp_path):
    run_dir = _run_dir(tmp_path)
    (run_dir / "sessions" / "2026" / "07" / "05" / "session-x.json").write_text("")
    assert gemini.to_atif(run_dir) is None


def test_tool_call_with_no_result(tmp_path):
    run_dir = _run_dir(tmp_path)
    _write_session(
        run_dir,
        {
            "sessionId": "x",
            "messages": [
                {"type": "user", "content": "go"},
                {
                    "type": "gemini",
                    "content": "",
                    "toolCalls": [{"id": "c1", "name": "run_shell", "args": {"command": "x"}}],
                },
            ],
        },
    )
    t = gemini.to_atif(run_dir)
    step = t.steps[1]
    assert step.tool_calls[0].tool_call_id == "c1"
    # observation result exists but content is None (no result yet)
    assert step.observation.results[0].content is None
    assert step.observation.results[0].source_call_id == "c1"


def test_gemini_message_without_tokens(tmp_path):
    run_dir = _run_dir(tmp_path)
    _write_session(
        run_dir,
        {"sessionId": "x", "messages": [{"type": "gemini", "content": "hi"}]},
    )
    t = gemini.to_atif(run_dir)
    assert t.steps[0].metrics is None
    assert t.final_metrics.total_prompt_tokens is None


def test_content_as_parts_list(tmp_path):
    run_dir = _run_dir(tmp_path)
    _write_session(
        run_dir,
        {
            "sessionId": "x",
            "messages": [
                {"type": "user", "content": [{"text": "line1"}, {"text": "line2"}]},
            ],
        },
    )
    # user-only session -> at least one user step
    t = gemini.to_atif(run_dir)
    assert t.steps[0].message == "line1\nline2"


def test_image_part_folds_to_placeholder(tmp_path):
    run_dir = _run_dir(tmp_path)
    _write_session(
        run_dir,
        {
            "sessionId": "x",
            "messages": [
                {"type": "user", "content": "screenshot?"},
                {
                    "type": "gemini",
                    "content": "",
                    "toolCalls": [
                        {
                            "id": "c1",
                            "name": "capture",
                            "args": {},
                            "result": [
                                {
                                    "functionResponse": {
                                        "response": {"output": "done"},
                                        "parts": [{"inlineData": {"mimeType": "image/png", "data": "AAAA"}}],
                                    }
                                }
                            ],
                        }
                    ],
                },
            ],
        },
    )
    t = gemini.to_atif(run_dir)
    content = t.steps[1].observation.results[0].content
    assert content == "done\n[image]"


def test_jsonl_rewind_drops_rewound_messages(tmp_path):
    run_dir = _run_dir(tmp_path)
    _write_session(
        run_dir,
        [
            {"$set": {"sessionId": "s"}},
            {"type": "user", "id": "m1", "content": "first"},
            {"type": "gemini", "id": "m2", "content": "bad path"},
            {"$rewindTo": "m2"},  # drops m2 (and everything from it)
            {"type": "gemini", "id": "m3", "content": "good path"},
        ],
    )
    t = gemini.to_atif(run_dir)
    msgs = [s.message for s in t.steps]
    assert "bad path" not in msgs
    assert "good path" in msgs
    assert "first" in msgs


def test_malformed_jsonl_line_skipped(tmp_path):
    run_dir = _run_dir(tmp_path)
    p = run_dir / "sessions" / "2026" / "07" / "05" / "session-x.json"
    p.write_text(
        json.dumps({"$set": {"sessionId": "s"}}) + "\n"
        "this is not json {\n" + json.dumps({"type": "user", "id": "m1", "content": "hi"}) + "\n"
    )
    t = gemini.to_atif(run_dir)
    assert t is not None
    assert t.steps[0].message == "hi"


def test_sregym_meta_absent_leaves_extra_unset(tmp_path):
    run_dir = _run_dir(tmp_path)
    _write_session(run_dir, {"sessionId": "x", "messages": [{"type": "user", "content": "hi"}]})
    t = gemini.to_atif(run_dir)
    assert t.extra is None


def test_roundtrip_with_tool_and_reasoning(tmp_path):
    run_dir = _run_dir(tmp_path)
    _write_session(
        run_dir,
        {
            "sessionId": "x",
            "messages": [
                {"type": "user", "content": "go"},
                {
                    "type": "gemini",
                    "content": "ok",
                    "thoughts": [{"description": "thinking"}],
                    "toolCalls": [
                        {
                            "id": "c1",
                            "name": "t",
                            "args": {},
                            "result": [{"functionResponse": {"response": {"output": "r"}}}],
                        }
                    ],
                    "tokens": {"input": 10, "output": 5, "cached": 0, "thoughts": 2, "tool": 1},
                },
            ],
        },
    )
    t = gemini.to_atif(run_dir)
    Trajectory.model_validate(json.loads(json.dumps(t.to_json_dict())))


def test_jsonl_message_update_applied(tmp_path):
    """A message_update record merges tokens + a tool call into an existing message."""
    run_dir = _run_dir(tmp_path)
    _write_session(
        run_dir,
        [
            {"$set": {"sessionId": "s"}},
            {"type": "user", "id": "m1", "content": "go"},
            {"type": "gemini", "id": "m2", "content": "patching"},
            {
                "type": "message_update",
                "id": "m2",
                "tokens": {"input": 800, "output": 20, "cached": 0, "thoughts": 10, "tool": 0},
                "toolCalls": [
                    {
                        "id": "c1",
                        "name": "run_shell",
                        "args": {"command": "kubectl patch"},
                        "result": [{"functionResponse": {"response": {"output": "patched"}}}],
                    }
                ],
            },
        ],
    )
    t = gemini.to_atif(run_dir)
    agent = next(s for s in t.steps if s.source == "agent")
    assert agent.tool_calls is not None and agent.tool_calls[0].tool_call_id == "c1"
    assert agent.observation.results[0].content == "patched"
    # completion = output + thoughts + tool = 20 + 10 + 0
    assert agent.metrics.completion_tokens == 30
    assert agent.metrics.prompt_tokens == 800
