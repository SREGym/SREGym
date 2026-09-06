"""Export ``traces.db`` to plain CSV/TSV for comparison across runs.

Three tables, because they answer different questions:

``runs``
    One row per trajectory. Totals and identity -- the table you sort to ask
    "which runs were expensive / long / unsuccessful".

``steps``
    One row per step. The timeline. Carries ``offset_s`` and ``step_frac``,
    which are what make runs comparable: absolute timestamps put two runs on
    different parts of the x-axis, so overlaying them is meaningless. Offset
    from each run's own first step puts them on top of each other, and the
    fraction handles runs of different lengths.

``tools``
    One row per tool call, for asking how two runs went about the same problem
    differently.

Cumulative columns (``cum_cost_usd``, ``cum_prompt_tokens``, ...) are
precomputed rather than left to the plotting tool, because a running total is
the one transform every graphing front-end makes awkward and every comparison
of "where did the budget go" needs.

Usage::

    python -m sregym.traces.export --db results/traces.db --out-dir /tmp/export
    python -m sregym.traces.export --table steps --stdout | ...
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sqlite3
import sys
from collections.abc import Iterator, Sequence
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("all.sregym.traces.export")

TABLES = ("runs", "steps", "tools")

RUN_COLUMNS = (
    "run_key",
    "trajectory_id",
    "problem_id",
    "application",
    "agent_name",
    "agent_version",
    "model_name",
    "batch",
    "run",
    "submitted",
    "diagnosis_submitted_step",
    "num_steps",
    "first_step_ts",
    "last_step_ts",
    "duration_s",
    "total_prompt_tokens",
    "total_completion_tokens",
    "total_cached_tokens",
    "total_cost_usd",
    "tool_call_count",
    "distinct_tool_count",
    "ingested_at",
)

STEP_COLUMNS = (
    "run_key",
    "trajectory_id",
    "step_id",
    "ts",
    "offset_s",
    "step_frac",
    "source",
    "model_name",
    "llm_call_count",
    "prompt_tokens",
    "completion_tokens",
    "cached_tokens",
    "cost_usd",
    "cum_prompt_tokens",
    "cum_completion_tokens",
    "cum_cost_usd",
    "tool_call_count",
    "tool_names",
    "message_chars",
)

TOOL_COLUMNS = (
    "run_key",
    "trajectory_id",
    "step_id",
    "offset_s",
    "seq",
    "function_name",
    "tool_call_id",
    "arguments_chars",
)


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        # Stored as ISO-8601 with a trailing Z, which fromisoformat handles
        # natively from 3.11.
        return datetime.fromisoformat(value)
    except ValueError:
        logger.debug(f"Unparseable timestamp {value!r}")
        return None


def _run_key(row: sqlite3.Row) -> str:
    """A stable, human-sortable identifier for one run.

    Deliberately readable rather than a UUID: it becomes the series label in
    whatever plots this data, and `trajectory_id` is unreadable in a legend.
    """
    parts = [
        row["problem_id"] or "unknown-problem",
        row["agent_name"] or "unknown-agent",
        row["model_name"] or "unknown-model",
    ]
    if row["batch"]:
        parts.append(str(row["batch"]))
    if row["run"] is not None:
        parts.append(f"run{row['run']}")
    # Disambiguate repeats of an otherwise identical key.
    parts.append((row["trajectory_id"] or "")[:8])
    return "/".join(parts)


def _connect(db_path: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _select_trajectories(
    conn: sqlite3.Connection,
    *,
    problem: str | None = None,
    agent: str | None = None,
    model: str | None = None,
) -> list[sqlite3.Row]:
    sql = "SELECT * FROM trajectories"
    clauses, params = [], []
    for column, value in (("problem_id", problem), ("agent_name", agent), ("model_name", model)):
        if value:
            clauses.append(f"{column} = ?")
            params.append(value)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY problem_id, agent_name, model_name, batch, run"
    return list(conn.execute(sql, params))


def _steps_for(conn: sqlite3.Connection, trajectory_id: str) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT * FROM steps WHERE trajectory_id = ? ORDER BY step_id",
            (trajectory_id,),
        )
    )


def _tool_calls_for(conn: sqlite3.Connection, step_pk: int) -> list[sqlite3.Row]:
    return list(conn.execute("SELECT * FROM tool_calls WHERE step_pk = ? ORDER BY seq", (step_pk,)))


def _message_chars(row: sqlite3.Row) -> int:
    text = row["message"] or ""
    if not text and row["message_parts"]:
        try:
            text = json.dumps(json.loads(row["message_parts"]))
        except (json.JSONDecodeError, TypeError):
            text = str(row["message_parts"])
    return len(text)


def iter_rows(
    db_path: Path | str,
    table: str,
    **filters,
) -> Iterator[dict]:
    """Yield export rows for one table.

    Streams rather than materialising: a step-level export of a long campaign is
    large, and the caller usually just writes it straight out.
    """
    if table not in TABLES:
        raise ValueError(f"Unknown table {table!r}; expected one of {TABLES}")

    with _connect(db_path) as conn:
        for traj in _select_trajectories(conn, **filters):
            key = _run_key(traj)
            steps = _steps_for(conn, traj["trajectory_id"])
            times = [_parse_ts(s["timestamp"]) for s in steps]
            known = [t for t in times if t is not None]
            first, last = (known[0], known[-1]) if known else (None, None)
            duration = (last - first).total_seconds() if first and last else None
            last_index = max(len(steps) - 1, 1)

            # Tool calls are needed for both the runs summary and the per-step
            # rows, so gather once.
            per_step_tools = {s["id"]: _tool_calls_for(conn, s["id"]) for s in steps}

            if table == "runs":
                all_tools = [t for calls in per_step_tools.values() for t in calls]
                yield {
                    "run_key": key,
                    "trajectory_id": traj["trajectory_id"],
                    "problem_id": traj["problem_id"],
                    "application": traj["application"],
                    "agent_name": traj["agent_name"],
                    "agent_version": traj["agent_version"],
                    "model_name": traj["model_name"],
                    "batch": traj["batch"],
                    "run": traj["run"],
                    "submitted": traj["submitted"],
                    "diagnosis_submitted_step": traj["diagnosis_submitted_step"],
                    "num_steps": traj["num_steps"],
                    "first_step_ts": first.isoformat() if first else None,
                    "last_step_ts": last.isoformat() if last else None,
                    "duration_s": round(duration, 3) if duration is not None else None,
                    "total_prompt_tokens": traj["total_prompt_tokens"],
                    "total_completion_tokens": traj["total_completion_tokens"],
                    "total_cached_tokens": traj["total_cached_tokens"],
                    "total_cost_usd": traj["total_cost_usd"],
                    "tool_call_count": len(all_tools),
                    "distinct_tool_count": len({t["function_name"] for t in all_tools}),
                    "ingested_at": traj["ingested_at"],
                }
                continue

            cum_prompt = cum_completion = 0
            cum_cost = 0.0
            for index, (step, ts) in enumerate(zip(steps, times, strict=True)):
                offset = (ts - first).total_seconds() if ts and first else None
                cum_prompt += step["prompt_tokens"] or 0
                cum_completion += step["completion_tokens"] or 0
                cum_cost += step["cost_usd"] or 0.0
                tools = per_step_tools[step["id"]]

                if table == "steps":
                    yield {
                        "run_key": key,
                        "trajectory_id": traj["trajectory_id"],
                        "step_id": step["step_id"],
                        "ts": step["timestamp"],
                        "offset_s": round(offset, 3) if offset is not None else None,
                        "step_frac": round(index / last_index, 4),
                        "source": step["source"],
                        "model_name": step["model_name"],
                        "llm_call_count": step["llm_call_count"],
                        "prompt_tokens": step["prompt_tokens"],
                        "completion_tokens": step["completion_tokens"],
                        "cached_tokens": step["cached_tokens"],
                        "cost_usd": step["cost_usd"],
                        "cum_prompt_tokens": cum_prompt,
                        "cum_completion_tokens": cum_completion,
                        "cum_cost_usd": round(cum_cost, 6),
                        "tool_call_count": len(tools),
                        # Pipe-joined: commas would need quoting in CSV and this
                        # column is read by humans and split by scripts.
                        "tool_names": "|".join(t["function_name"] or "" for t in tools),
                        "message_chars": _message_chars(step),
                    }
                else:  # tools
                    for tool in tools:
                        yield {
                            "run_key": key,
                            "trajectory_id": traj["trajectory_id"],
                            "step_id": step["step_id"],
                            "offset_s": round(offset, 3) if offset is not None else None,
                            "seq": tool["seq"],
                            "function_name": tool["function_name"],
                            "tool_call_id": tool["tool_call_id"],
                            "arguments_chars": len(tool["arguments"] or ""),
                        }


def columns_for(table: str) -> Sequence[str]:
    return {"runs": RUN_COLUMNS, "steps": STEP_COLUMNS, "tools": TOOL_COLUMNS}[table]


def write_table(db_path: Path | str, table: str, handle, *, delimiter: str = ",", **filters) -> int:
    """Write one table to an open text handle. Returns the row count."""
    writer = csv.DictWriter(handle, fieldnames=list(columns_for(table)), delimiter=delimiter, extrasaction="raise")
    writer.writeheader()
    count = 0
    for row in iter_rows(db_path, table, **filters):
        writer.writerow(row)
        count += 1
    return count


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Export traces.db to CSV/TSV for cross-run comparison.",
    )
    parser.add_argument("--db", default="results/traces.db", help="Path to traces.db (default: results/traces.db)")
    parser.add_argument("--out-dir", default=None, help="Directory to write <table>.csv files into")
    parser.add_argument("--stdout", action="store_true", help="Write a single table to stdout instead of files")
    parser.add_argument(
        "--table",
        choices=TABLES,
        default=None,
        help="Table to export (required with --stdout; default: all three)",
    )
    parser.add_argument("--format", choices=("csv", "tsv"), default="csv")
    parser.add_argument("--problem", default=None, help="Only runs for this problem_id")
    parser.add_argument("--agent", default=None, help="Only runs for this agent_name")
    parser.add_argument("--model", default=None, help="Only runs for this model_name")
    args = parser.parse_args(argv)

    if not Path(args.db).exists():
        parser.error(f"No such database: {args.db}")
    if args.stdout and args.table is None:
        parser.error("--stdout needs --table (one table per stream)")
    if not args.stdout and args.out_dir is None:
        parser.error("Pass --out-dir, or --stdout with --table")

    delimiter = "\t" if args.format == "tsv" else ","
    filters = {"problem": args.problem, "agent": args.agent, "model": args.model}

    if args.stdout:
        write_table(args.db, args.table, sys.stdout, delimiter=delimiter, **filters)
        return 0

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    suffix = "tsv" if args.format == "tsv" else "csv"
    for table in [args.table] if args.table else TABLES:
        path = out_dir / f"{table}.{suffix}"
        with open(path, "w", encoding="utf-8", newline="") as handle:
            n = write_table(args.db, table, handle, delimiter=delimiter, **filters)
        print(f"{path}: {n} rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
