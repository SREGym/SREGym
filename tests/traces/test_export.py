"""The exporter exists to make runs comparable, so that is what these check.

The load-bearing columns are ``offset_s`` and ``step_frac``: without them two
runs sit on different parts of an absolute time axis and cannot be overlaid.
Everything else is bookkeeping.
"""

import csv
import io
import json
import sqlite3

import pytest

from atif_converter import Trajectory
from sregym.traces import export, store


def _make_db(path, runs):
    """Build a traces.db with the real schema and the given synthetic runs."""
    store.init_db(path)
    conn = sqlite3.connect(path)
    for r in runs:
        conn.execute(
            """INSERT INTO trajectories
               (trajectory_id, schema_version, agent_name, model_name, problem_id,
                application, batch, run, num_steps, total_prompt_tokens,
                total_completion_tokens, total_cost_usd, submitted)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                r["id"],
                "1.0",
                r.get("agent", "stratus"),
                r.get("model", "opus"),
                r["problem"],
                r.get("app", "hotel-reservation"),
                r.get("batch", "b1"),
                r.get("run", 1),
                len(r["steps"]),
                sum(s.get("pt", 0) for s in r["steps"]),
                sum(s.get("ct", 0) for s in r["steps"]),
                r.get("cost", 1.0),
                1,
            ),
        )
        for i, s in enumerate(r["steps"], start=1):
            cur = conn.execute(
                """INSERT INTO steps
                   (trajectory_id, step_id, timestamp, source, prompt_tokens,
                    completion_tokens, cost_usd, message)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (
                    r["id"],
                    i,
                    s["ts"],
                    s.get("source", "assistant"),
                    s.get("pt", 0),
                    s.get("ct", 0),
                    s.get("cost"),
                    s.get("msg", "hi"),
                ),
            )
            for seq, tool in enumerate(s.get("tools", []), start=1):
                conn.execute(
                    """INSERT INTO tool_calls (step_pk, seq, tool_call_id, function_name, arguments)
                       VALUES (?,?,?,?,?)""",
                    (cur.lastrowid, seq, f"tc{seq}", tool, '{"a":1}'),
                )
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def db(tmp_path):
    """Two runs of the same problem, hours apart and of different lengths.

    That is the comparison case: absolute times differ, step counts differ.
    """
    return _make_db(
        tmp_path / "traces.db",
        [
            {
                "id": "traj-a",
                "problem": "wrong_selector",
                "run": 1,
                "steps": [
                    {"ts": "2026-09-01T10:00:00.000Z", "pt": 100, "ct": 10, "tools": ["get_logs"]},
                    {"ts": "2026-09-01T10:00:30.000Z", "pt": 200, "ct": 20},
                    {"ts": "2026-09-01T10:01:00.000Z", "pt": 300, "ct": 30, "tools": ["kubectl", "submit"]},
                ],
            },
            {
                "id": "traj-b",
                "problem": "wrong_selector",
                "run": 2,
                "steps": [
                    {"ts": "2026-09-01T18:00:00.000Z", "pt": 50, "ct": 5},
                    {"ts": "2026-09-01T18:00:20.000Z", "pt": 60, "ct": 6},
                ],
            },
        ],
    )


def _rows(db, table, **kw):
    return list(export.iter_rows(db, table, **kw))


def test_runs_table_has_one_row_per_trajectory(db):
    rows = _rows(db, "runs")
    assert len(rows) == 2
    assert {r["trajectory_id"] for r in rows} == {"traj-a", "traj-b"}


def test_run_duration_spans_first_to_last_step(db):
    a = next(r for r in _rows(db, "runs") if r["trajectory_id"] == "traj-a")
    assert a["duration_s"] == pytest.approx(60.0)
    assert a["first_step_ts"].startswith("2026-09-01T10:00:00")


def test_run_key_is_readable_and_unique(db):
    """It becomes the series label in a plot, so it has to be legible."""
    keys = [r["run_key"] for r in _rows(db, "runs")]
    assert all("wrong_selector" in k and "stratus" in k for k in keys)
    assert len(set(keys)) == 2


def test_offset_s_is_relative_to_each_run_own_start(db):
    """The point of the whole exercise: runs 8 hours apart both start at 0."""
    steps = _rows(db, "steps")
    a = [s["offset_s"] for s in steps if s["trajectory_id"] == "traj-a"]
    b = [s["offset_s"] for s in steps if s["trajectory_id"] == "traj-b"]
    assert a == [0.0, 30.0, 60.0]
    assert b == [0.0, 20.0]


def test_step_frac_spans_zero_to_one_regardless_of_length(db):
    steps = _rows(db, "steps")
    for tid in ("traj-a", "traj-b"):
        fracs = [s["step_frac"] for s in steps if s["trajectory_id"] == tid]
        assert fracs[0] == 0.0
        assert fracs[-1] == 1.0


def test_cumulative_tokens_are_monotonic_and_match_the_total(db):
    steps = [s for s in _rows(db, "steps") if s["trajectory_id"] == "traj-a"]
    cum = [s["cum_prompt_tokens"] for s in steps]
    assert cum == sorted(cum)
    run = next(r for r in _rows(db, "runs") if r["trajectory_id"] == "traj-a")
    assert cum[-1] == run["total_prompt_tokens"]


def test_null_per_step_cost_stays_zero_rather_than_being_invented(db):
    """Real databases have no per-step cost; we must not synthesise it."""
    steps = [s for s in _rows(db, "steps") if s["trajectory_id"] == "traj-a"]
    assert all(s["cum_cost_usd"] == 0 for s in steps)


def test_tools_table_has_one_row_per_call(db):
    tools = _rows(db, "tools")
    assert len(tools) == 3  # get_logs, kubectl, submit
    assert {t["function_name"] for t in tools} == {"get_logs", "kubectl", "submit"}
    # Tool calls carry the run-relative offset so they can be pinned on a timeline.
    assert all(t["offset_s"] is not None for t in tools)


def test_steps_carry_pipe_joined_tool_names(db):
    steps = _rows(db, "steps")
    multi = next(s for s in steps if s["tool_call_count"] == 2)
    assert multi["tool_names"] == "kubectl|submit"


def test_run_summary_counts_distinct_tools(db):
    a = next(r for r in _rows(db, "runs") if r["trajectory_id"] == "traj-a")
    assert a["tool_call_count"] == 3
    assert a["distinct_tool_count"] == 3


def test_filters_narrow_the_export(db):
    assert len(_rows(db, "runs", problem="wrong_selector")) == 2
    assert len(_rows(db, "runs", problem="nope")) == 0
    assert len(_rows(db, "runs", agent="stratus")) == 2
    assert len(_rows(db, "runs", agent="claudecode")) == 0


def test_unknown_table_is_rejected(db):
    with pytest.raises(ValueError, match="Unknown table"):
        list(export.iter_rows(db, "bananas"))


def test_written_csv_has_declared_stable_columns(db):
    """Unlike the results CSV, the header must not depend on the data."""
    buf = io.StringIO()
    export.write_table(db, "steps", buf)
    header = next(csv.reader(io.StringIO(buf.getvalue())))
    assert header == list(export.STEP_COLUMNS)


def test_tsv_delimiter(db):
    buf = io.StringIO()
    export.write_table(db, "runs", buf, delimiter="\t")
    assert "\t" in buf.getvalue().splitlines()[0]


def test_export_does_not_modify_the_database(db):
    before = db.read_bytes()
    for table in export.TABLES:
        list(export.iter_rows(db, table))
    assert db.read_bytes() == before


@pytest.mark.parametrize("exit_mode", ["complete", "error", "early_close"])
def test_export_closes_connection(db, monkeypatch, exit_mode):
    conn = export._connect(db)
    monkeypatch.setattr(export, "_connect", lambda _: conn)
    rows = export.iter_rows(db, "runs")
    try:
        if exit_mode == "error":
            conn.set_authorizer(lambda *_: sqlite3.SQLITE_DENY)
            with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
                next(rows)
        elif exit_mode == "early_close":
            next(rows)
            rows.close()
        else:
            list(rows)

        with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
            conn.execute("SELECT 1")
    finally:
        rows.close()
        conn.close()


def test_cli_requires_a_table_for_stdout(db, capsys):
    with pytest.raises(SystemExit):
        export.main(["--db", str(db), "--stdout"])


def test_cli_writes_all_three_tables(db, tmp_path, capsys):
    out = tmp_path / "out"
    assert export.main(["--db", str(db), "--out-dir", str(out)]) == 0
    for table in export.TABLES:
        assert (out / f"{table}.csv").exists()


def test_cli_rejects_a_missing_database(tmp_path):
    with pytest.raises(SystemExit):
        export.main(["--db", str(tmp_path / "nope.db"), "--out-dir", str(tmp_path)])


def test_cli_exports_binary_json_from_the_real_store(tmp_path):
    parts = [
        {"type": "text", "text": "look at café"},
        {"type": "image", "source": {"media_type": "image/png", "path": "screen.png"}},
    ]
    arguments = {"cmd": "printf café"}
    trajectory = Trajectory.model_validate(
        {
            "schema_version": "ATIF-v1.7",
            "trajectory_id": "binary-json",
            "agent": {"name": "codex", "version": "1.0"},
            "steps": [
                {"step_id": 1, "source": "user", "message": parts},
                {
                    "step_id": 2,
                    "source": "agent",
                    "message": "checking",
                    "tool_calls": [{"tool_call_id": "c1", "function_name": "shell", "arguments": arguments}],
                },
            ],
        }
    )
    db = tmp_path / "traces.db"
    store.upsert(trajectory, db)
    before = db.read_bytes()
    out = tmp_path / "export"

    assert export.main(["--db", str(db), "--out-dir", str(out)]) == 0

    with (out / "steps.csv").open(newline="") as handle:
        steps = list(csv.DictReader(handle))
    with (out / "tools.csv").open(newline="") as handle:
        tools = list(csv.DictReader(handle))
    assert int(steps[0]["message_chars"]) == len(json.dumps(parts))
    assert int(tools[0]["arguments_chars"]) == len(json.dumps(arguments, ensure_ascii=False, separators=(",", ":")))
    assert (out / "runs.csv").exists()
    assert db.read_bytes() == before


@pytest.mark.parametrize(
    ("timestamps", "duration", "offsets"),
    [
        (["2026-09-08T10:00:00", "2026-09-08T10:00:30Z"], None, [0.0, None]),
        (["2026-09-08T10:00:00Z", "2026-09-08T10:00:30"], None, [0.0, None]),
        (["2026-09-08T10:00:00Z", "2026-09-08T10:00:15", "2026-09-08T10:00:30Z"], 30.0, [0.0, None, 30.0]),
        (["2026-09-08T10:00:00+02:00", "2026-09-08T08:00:30Z"], 30.0, [0.0, 30.0]),
        (["2026-09-08T10:00:00", "2026-09-08T10:00:30"], 30.0, [0.0, 30.0]),
        ([None, "2026-09-08T10:00:00Z", "2026-09-08T10:00:30Z"], 30.0, [None, 0.0, 30.0]),
        ([None, None], None, [None, None]),
    ],
)
def test_export_handles_timestamp_variants(tmp_path, timestamps, duration, offsets):
    trajectory = Trajectory.model_validate(
        {
            "schema_version": "ATIF-v1.7",
            "trajectory_id": "timestamps",
            "agent": {"name": "codex", "version": "1.0"},
            "steps": [
                {
                    "step_id": index,
                    "source": "agent",
                    "timestamp": timestamp,
                    "message": "checking",
                    "tool_calls": [{"tool_call_id": f"c{index}", "function_name": "shell", "arguments": {}}],
                }
                for index, timestamp in enumerate(timestamps, start=1)
            ],
        }
    )
    db = tmp_path / "traces.db"
    store.upsert(trajectory, db)

    assert _rows(db, "runs")[0]["duration_s"] == duration
    assert [row["offset_s"] for row in _rows(db, "steps")] == offsets
    assert [row["offset_s"] for row in _rows(db, "tools")] == offsets
