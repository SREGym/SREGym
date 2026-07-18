"""Edge-case coverage for the Claude Code adapter + convert dispatch.

These build tiny synthetic session logs (a few JSONL lines each) to exercise
branches the large golden fixture never hits: the false-positive submission
substring, the no-submission path, orphan tool_results, multiple session dirs,
and unreadable/malformed lines.
"""

import json
from pathlib import Path

from sregym.traces import convert
from sregym.traces.adapters import claudecode
from sregym.traces.atif import Trajectory


def _write_session(run_dir: Path, records: list[dict], *, project: str = "-app") -> None:
    """Materialize ``records`` as a Claude session log under ``run_dir``."""
    logs = run_dir / "sessions" / "projects" / project
    logs.mkdir(parents=True, exist_ok=True)
    with (logs / "session.jsonl").open("w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec) + "\n")


def _assistant(msg_id: str, content: list[dict], *, ts: str, usage: dict | None = None) -> dict:
    return {
        "type": "assistant",
        "uuid": f"a-{msg_id}-{ts}",
        "timestamp": ts,
        "sessionId": "sess",
        "version": "9.9.9",
        "message": {
            "id": msg_id,
            "role": "assistant",
            "model": "claude-sonnet-4-6",
            "content": content,
            "usage": usage or {"input_tokens": 1, "output_tokens": 2},
        },
    }


def _tool_result(call_id: str, content: str, *, ts: str, is_error: bool = False) -> dict:
    return {
        "type": "user",
        "uuid": f"u-{call_id}-{ts}",
        "timestamp": ts,
        "sessionId": "sess",
        "message": {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": call_id,
                    "content": content,
                    "is_error": is_error,
                }
            ],
        },
        "toolUseResult": {"stdout": content, "stderr": "", "interrupted": False, "isImage": False},
    }


def test_submission_substring_in_tool_output_is_not_a_boundary(tmp_path):
    # An agent greps a prior log that merely *contains* the phrase. This must
    # NOT be treated as a submission boundary (no structured envelope).
    run_dir = tmp_path / "results" / "b" / "claudecode" / "p_hotel_reservation" / "run_1"
    run_dir.mkdir(parents=True)
    _write_session(
        run_dir,
        [
            _assistant(
                "m1",
                [{"type": "tool_use", "id": "c1", "name": "Bash", "input": {"command": "grep x log"}}],
                ts="2026-06-29T05:00:00.000Z",
            ),
            _tool_result("c1", "old log line: Submission received earlier", ts="2026-06-29T05:00:01.000Z"),
        ],
    )
    traj = convert.convert_run(run_dir)
    assert traj is not None
    assert "diagnosis_submitted_step" not in traj.extra["sregym"]


def test_real_submission_envelope_is_detected(tmp_path):
    run_dir = tmp_path / "results" / "b" / "claudecode" / "p_hotel_reservation" / "run_1"
    run_dir.mkdir(parents=True)
    _write_session(
        run_dir,
        [
            _assistant(
                "m1",
                [{"type": "tool_use", "id": "c1", "name": "Bash", "input": {"command": "curl submit"}}],
                ts="2026-06-29T05:00:00.000Z",
            ),
            _tool_result(
                "c1",
                '{"status":"200","message":"Submission received"}',
                ts="2026-06-29T05:00:01.000Z",
            ),
        ],
    )
    traj = convert.convert_run(run_dir)
    assert traj.extra["sregym"]["diagnosis_submitted_step"] == 1


def test_no_submission_still_has_complete_sregym_meta(tmp_path):
    # Even with no submission, extra.sregym must be assembled from a single
    # source and carry the path-derived keys (regression guard for the
    # "extra only set inside the boundary branch" bug).
    run_dir = tmp_path / "results" / "0629_x" / "claudecode" / "p_social_network" / "run_2"
    run_dir.mkdir(parents=True)
    _write_session(
        run_dir,
        [_assistant("m1", [{"type": "text", "text": "thinking..."}], ts="2026-06-29T05:00:00.000Z")],
    )
    traj = convert.convert_run(run_dir)
    sregym = traj.extra["sregym"]
    assert sregym["problem_id"] == "p_social_network"
    assert sregym["application"] == "Social Network"
    assert sregym["run"] == 2
    assert "diagnosis_submitted_step" not in sregym


def test_orphan_tool_result_becomes_a_step(tmp_path):
    # A tool_result whose tool_use never appeared (e.g. replayed after
    # compaction) but that carries a tool name is preserved as a synthetic step.
    run_dir = tmp_path / "results" / "b" / "claudecode" / "kubelet_crash" / "run_1"
    run_dir.mkdir(parents=True)
    _write_session(
        run_dir,
        [
            _assistant("m1", [{"type": "text", "text": "hi"}], ts="2026-06-29T05:00:00.000Z"),
            {
                "type": "user",
                "uuid": "orphan-1",
                "timestamp": "2026-06-29T05:00:01.000Z",
                "sessionId": "sess",
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "ghost",
                            "name": "Bash",
                            "content": "orphan output",
                            "is_error": False,
                        }
                    ],
                },
            },
        ],
    )
    traj = claudecode.to_atif(run_dir)
    assert traj is not None
    Trajectory.model_validate(traj.to_json_dict())
    ghost = [s for s in traj.steps if s.tool_calls and any(tc.tool_call_id == "ghost" for tc in s.tool_calls)]
    assert len(ghost) == 1
