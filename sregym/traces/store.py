"""SQLite store for normalized ATIF trajectories, indexed by problem type.

Part two of the "uniform agent trace" work. Once a run's raw logs are normalized
into a validated ATIF ``trajectory.json`` (see ``sregym.traces.postprocess``),
this module ingests those trajectories into a single SQLite database so callers
can query by problem type / agent / application, and drill into reasoning traces
and tool calls **with SQL** instead of writing another parser.

Design:

* Normalized relational schema mirroring ATIF: a trajectory has many steps, a
  step has many tool_calls and many observation_results. Everything ATIF fixes
  (a defined name and type) is a real typed column; the irreducibly variable /
  array-of-scalar leaves live in ``JSONB`` columns (SQLite 3.45+ binary JSON).
  No trajectory data is dropped -- every ATIF field maps to a column or a JSONB
  column, and ``get()`` reconstructs an identical ``Trajectory``.
* Embedded ``subagent_trajectories`` are stored as their own trajectory rows
  linked by ``parent_trajectory_id`` and re-nested on read.
* Idempotent: ``trajectory_id`` (the canonical results path) is the primary key;
  re-ingesting deletes the trajectory's child rows via ``ON DELETE CASCADE`` and
  re-inserts, so a rerun is a clean replace.

The DB is a derived, rebuildable index -- the ``trajectory.json`` files remain
the source of truth.

Usage::

    python -m sregym.traces.store ingest results/            # walk tree, ingest
    python -m sregym.traces.store --db traces.db ingest results/
    python -m sregym.traces.store list --problem service_port_conflict_hotel_reservation
    python -m sregym.traces.store stats
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from atif_converter import (
    Agent,
    ContentPart,
    FinalMetrics,
    Metrics,
    Observation,
    ObservationResult,
    Step,
    SubagentTrajectoryRef,
    ToolCall,
    Trajectory,
)

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = Path("traces.db")

# The pipeline (main.py) auto-ingests to ``results/traces.db``. The CLI defaults
# to the same file so ``list``/``stats`` see the auto-ingested data without a
# ``--db`` flag.
CLI_DEFAULT_DB_PATH = Path("results") / DEFAULT_DB_PATH

# Every ATIF field lands in the DB. Fields ATIF fixes (defined name +
# type) are typed columns; the JSONB columns below hold the leaves that CANNOT
# be fixed columns -- not omissions, just the irreducibly variable shapes:
#   * arguments / *_extra      -> free-form / open-ended objects (per tool, custom)
#   * message_parts / content_parts -> multimodal ContentPart[] (mixed text+image)
#   * subagent_ref             -> SubagentTrajectoryRef[]
#   * tool_definitions         -> OpenAI-style function defs (variable)
#   * prompt/completion_token_ids, logprobs -> variable-length per-token arrays
#     (a per-token table would be 10^4+ rows per step; add one only if RL tooling
#     needs per-token SQL -- see open question 4 in the plan).
_SCHEMA = """
CREATE TABLE IF NOT EXISTS trajectories (
    trajectory_id            TEXT PRIMARY KEY,
    parent_trajectory_id     TEXT REFERENCES trajectories(trajectory_id) ON DELETE CASCADE,
    sibling_seq              INTEGER,            -- order within parent's subagent_trajectories[]
    session_id               TEXT,
    schema_version           TEXT NOT NULL,
    agent_name               TEXT NOT NULL,
    agent_version            TEXT,
    model_name               TEXT,
    problem_id               TEXT,
    application              TEXT,
    batch                    TEXT,
    run                      INTEGER,
    results_path             TEXT,
    submitted                INTEGER,
    diagnosis_submitted_step INTEGER,
    num_steps                INTEGER NOT NULL,
    total_prompt_tokens      INTEGER,
    total_completion_tokens  INTEGER,
    total_cached_tokens      INTEGER,
    total_cost_usd           REAL,
    total_steps              INTEGER,
    notes                    TEXT,
    continued_trajectory_ref TEXT,
    tool_definitions         JSONB,
    agent_extra              JSONB,
    final_metrics_extra      JSONB,
    extra                    JSONB,
    ingested_at              TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS steps (
    id                   INTEGER PRIMARY KEY,
    trajectory_id        TEXT NOT NULL REFERENCES trajectories(trajectory_id) ON DELETE CASCADE,
    step_id              INTEGER NOT NULL,
    timestamp            TEXT,
    source               TEXT NOT NULL,
    model_name           TEXT,
    reasoning_effort     TEXT,
    message              TEXT,
    message_parts        JSONB,
    reasoning_content    TEXT,
    llm_call_count       INTEGER,
    is_copied_context    INTEGER,
    prompt_tokens        INTEGER,
    completion_tokens    INTEGER,
    cached_tokens        INTEGER,
    cost_usd             REAL,
    prompt_token_ids     JSONB,
    completion_token_ids JSONB,
    logprobs             JSONB,
    metrics_extra        JSONB,
    extra                JSONB,
    UNIQUE (trajectory_id, step_id)
);

CREATE TABLE IF NOT EXISTS tool_calls (
    id            INTEGER PRIMARY KEY,
    step_pk       INTEGER NOT NULL REFERENCES steps(id) ON DELETE CASCADE,
    seq           INTEGER NOT NULL,
    tool_call_id  TEXT NOT NULL,
    function_name TEXT NOT NULL,
    arguments     JSONB NOT NULL,
    extra         JSONB
);

CREATE TABLE IF NOT EXISTS observation_results (
    id             INTEGER PRIMARY KEY,
    step_pk        INTEGER NOT NULL REFERENCES steps(id) ON DELETE CASCADE,
    seq            INTEGER NOT NULL,
    source_call_id TEXT,
    content        TEXT,
    content_parts  JSONB,
    subagent_ref   JSONB,
    extra          JSONB
);

CREATE INDEX IF NOT EXISTS idx_traj_problem ON trajectories(problem_id);
CREATE INDEX IF NOT EXISTS idx_traj_agent   ON trajectories(agent_name);
CREATE INDEX IF NOT EXISTS idx_traj_app     ON trajectories(application);
CREATE INDEX IF NOT EXISTS idx_traj_parent  ON trajectories(parent_trajectory_id);
CREATE INDEX IF NOT EXISTS idx_steps_traj   ON steps(trajectory_id);
CREATE INDEX IF NOT EXISTS idx_steps_source ON steps(source);
CREATE INDEX IF NOT EXISTS idx_tc_step      ON tool_calls(step_pk);
CREATE INDEX IF NOT EXISTS idx_tc_fn        ON tool_calls(function_name);
CREATE INDEX IF NOT EXISTS idx_obs_step     ON observation_results(step_pk);
"""


@dataclass(frozen=True)
class TrajectorySummary:
    """Lightweight row view for listings (no full payload)."""

    trajectory_id: str
    agent: str
    problem_id: str | None
    application: str | None
    run: int | None
    submitted: bool | None
    num_steps: int
    total_cost_usd: float | None


@contextmanager
def connect(db_path: Path | str = DEFAULT_DB_PATH) -> Iterator[sqlite3.Connection]:
    """Open a connection with schema ensured, FKs on, rows as dicts."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        conn.executescript(_SCHEMA)
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db(db_path: Path | str = DEFAULT_DB_PATH) -> None:
    """Create the database file and schema if absent (no-op if present)."""
    with connect(db_path):
        pass


# --- JSONB helpers ----------------------------------------------------------
#
# Writes bind a json.dumps string wrapped by SQLite's jsonb() function; reads
# pull it back with json() -> text -> json.loads. Callers doing SQL drill-down
# use json_extract(col, '$.path') / col ->> '$.path' directly on the blob.


def _dump(value: Any) -> str | None:
    """JSON-encode a value for a ``jsonb(?)`` bind, or None to store SQL NULL."""
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False)


def _batch_from_results_path(results_path: str | None) -> str | None:
    """First path segment of ``<batch>/<tool>/<problem>/run_<n>``."""
    if not results_path:
        return None
    parts = Path(results_path).parts
    return parts[0] if parts else None


def _message_columns(message: str | list[ContentPart]) -> tuple[str | None, str | None]:
    """Split an ATIF ``message`` into (plain_text, parts_json)."""
    if isinstance(message, str):
        return message, None
    return None, _dump([p.model_dump(exclude_none=True) for p in message])


def _content_columns(content: Any) -> tuple[str | None, str | None]:
    """Split an ObservationResult ``content`` into (plain_text, parts_json)."""
    if content is None:
        return None, None
    if isinstance(content, str):
        return content, None
    # list[ContentPart]
    return None, _dump([p.model_dump(exclude_none=True) for p in content])


# --- Write path -------------------------------------------------------------


def _insert_trajectory(
    conn: sqlite3.Connection,
    trajectory: Trajectory,
    *,
    parent_trajectory_id: str | None,
    sibling_seq: int | None = None,
) -> None:
    """Insert one trajectory + its steps/tool_calls/observations; recurse subagents."""
    if not trajectory.trajectory_id:
        raise ValueError("Trajectory.trajectory_id is required for storage")

    sregym = (trajectory.extra or {}).get("sregym", {})
    if not isinstance(sregym, dict):
        sregym = {}
    agent = trajectory.agent
    fm = trajectory.final_metrics
    submitted = sregym.get("submitted")

    conn.execute(
        """
        INSERT INTO trajectories (
            trajectory_id, parent_trajectory_id, sibling_seq, session_id, schema_version,
            agent_name, agent_version, model_name, problem_id, application,
            batch, run, results_path, submitted, diagnosis_submitted_step,
            num_steps, total_prompt_tokens, total_completion_tokens,
            total_cached_tokens, total_cost_usd, total_steps, notes,
            continued_trajectory_ref, tool_definitions, agent_extra,
            final_metrics_extra, extra
        ) VALUES (
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
            jsonb(?), jsonb(?), jsonb(?), jsonb(?)
        )
        """,
        (
            trajectory.trajectory_id,
            parent_trajectory_id,
            sibling_seq,
            trajectory.session_id,
            trajectory.schema_version,
            agent.name,
            agent.version,
            agent.model_name,
            sregym.get("problem_id"),
            sregym.get("application"),
            _batch_from_results_path(sregym.get("results_path")),
            sregym.get("run"),
            sregym.get("results_path"),
            None if submitted is None else int(bool(submitted)),
            sregym.get("diagnosis_submitted_step"),
            len(trajectory.steps),
            fm.total_prompt_tokens if fm else None,
            fm.total_completion_tokens if fm else None,
            fm.total_cached_tokens if fm else None,
            fm.total_cost_usd if fm else None,
            fm.total_steps if fm else None,
            trajectory.notes,
            trajectory.continued_trajectory_ref,
            _dump(agent.tool_definitions),
            _dump(agent.extra),
            _dump(fm.extra if fm else None),
            _dump(trajectory.extra),
        ),
    )

    for step in trajectory.steps:
        _insert_step(conn, trajectory.trajectory_id, step)

    for seq, sub in enumerate(trajectory.subagent_trajectories or []):
        _insert_trajectory(conn, sub, parent_trajectory_id=trajectory.trajectory_id, sibling_seq=seq)


def _insert_step(conn: sqlite3.Connection, trajectory_id: str, step: Step) -> None:
    message, message_parts = _message_columns(step.message)
    m = step.metrics
    cursor = conn.execute(
        """
        INSERT INTO steps (
            trajectory_id, step_id, timestamp, source, model_name,
            reasoning_effort, message, message_parts, reasoning_content,
            llm_call_count, is_copied_context, prompt_tokens, completion_tokens,
            cached_tokens, cost_usd, prompt_token_ids, completion_token_ids,
            logprobs, metrics_extra, extra
        ) VALUES (
            ?, ?, ?, ?, ?, ?, ?, jsonb(?), ?, ?, ?, ?, ?, ?, ?,
            jsonb(?), jsonb(?), jsonb(?), jsonb(?), jsonb(?)
        )
        """,
        (
            trajectory_id,
            step.step_id,
            step.timestamp,
            step.source,
            step.model_name,
            step.reasoning_effort,
            message,
            message_parts,
            step.reasoning_content,
            step.llm_call_count,
            None if step.is_copied_context is None else int(step.is_copied_context),
            m.prompt_tokens if m else None,
            m.completion_tokens if m else None,
            m.cached_tokens if m else None,
            m.cost_usd if m else None,
            _dump(m.prompt_token_ids if m else None),
            _dump(m.completion_token_ids if m else None),
            _dump(m.logprobs if m else None),
            _dump(m.extra if m else None),
            _dump(step.extra),
        ),
    )
    step_pk = cursor.lastrowid

    for seq, tc in enumerate(step.tool_calls or []):
        conn.execute(
            """
            INSERT INTO tool_calls (step_pk, seq, tool_call_id, function_name, arguments, extra)
            VALUES (?, ?, ?, ?, jsonb(?), jsonb(?))
            """,
            (step_pk, seq, tc.tool_call_id, tc.function_name, _dump(tc.arguments), _dump(tc.extra)),
        )

    if step.observation is not None:
        for seq, res in enumerate(step.observation.results):
            content, content_parts = _content_columns(res.content)
            subagent_ref = (
                _dump([r.model_dump(exclude_none=True) for r in res.subagent_trajectory_ref])
                if res.subagent_trajectory_ref
                else None
            )
            conn.execute(
                """
                INSERT INTO observation_results (
                    step_pk, seq, source_call_id, content, content_parts, subagent_ref, extra
                ) VALUES (?, ?, ?, ?, jsonb(?), jsonb(?), jsonb(?))
                """,
                (step_pk, seq, res.source_call_id, content, content_parts, subagent_ref, _dump(res.extra)),
            )


def upsert(trajectory: Trajectory, db_path: Path | str = DEFAULT_DB_PATH) -> str:
    """Insert or replace one trajectory (and its subagents); return its id."""
    with connect(db_path) as conn:
        # Delete-then-insert (child rows cascade) so a rerun is a clean replace.
        conn.execute("DELETE FROM trajectories WHERE trajectory_id = ?", (trajectory.trajectory_id,))
        _insert_trajectory(conn, trajectory, parent_trajectory_id=None)
    return trajectory.trajectory_id or ""


def ingest_trajectory_file(path: Path | str, db_path: Path | str = DEFAULT_DB_PATH) -> str | None:
    """Load, validate, and store one ``trajectory.json``; return its id or None."""
    path = Path(path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        trajectory = Trajectory.model_validate(payload)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        logger.warning("Skipping %s: %s", path, exc)
        return None
    return upsert(trajectory, db_path)


def ingest_tree(root: Path | str, db_path: Path | str = DEFAULT_DB_PATH) -> list[str]:
    """Ingest every ``trajectory.json`` under ``root`` (or a single file).

    Returns the list of stored root ``trajectory_id``s.
    """
    root = Path(root)
    files = [root] if root.is_file() else sorted(root.rglob("trajectory.json"))
    stored: list[str] = []
    for f in files:
        tid = ingest_trajectory_file(f, db_path)
        if tid is not None:
            stored.append(tid)
    return stored


# --- Read path (row -> ATIF model) ------------------------------------------


def _load(value: Any) -> Any:
    """Decode a value read as ``json(col)`` text back into Python, or None."""
    if value is None:
        return None
    return json.loads(value)


def _build_metrics(row: sqlite3.Row) -> Metrics | None:
    """Reassemble per-step Metrics from step columns, or None if all empty.

    JSONB columns arrive already decoded to JSON text (the step SELECT wraps them
    in ``json(col)``), so ``_load`` just parses the text.
    """
    fields = {
        "prompt_tokens": row["prompt_tokens"],
        "completion_tokens": row["completion_tokens"],
        "cached_tokens": row["cached_tokens"],
        "cost_usd": row["cost_usd"],
        "prompt_token_ids": _load(row["prompt_token_ids"]),
        "completion_token_ids": _load(row["completion_token_ids"]),
        "logprobs": _load(row["logprobs"]),
        "extra": _load(row["metrics_extra"]),
    }
    if all(v is None for v in fields.values()):
        return None
    return Metrics(**fields)


# Every read SELECT wraps JSONB columns in ``json(col) AS col`` so the fetched
# row already carries decodable JSON text -- no per-column re-fetch.
_STEP_COLS = (
    "id, step_id, timestamp, source, model_name, reasoning_effort, message, "
    "json(message_parts) AS message_parts, reasoning_content, llm_call_count, "
    "is_copied_context, prompt_tokens, completion_tokens, cached_tokens, cost_usd, "
    "json(prompt_token_ids) AS prompt_token_ids, "
    "json(completion_token_ids) AS completion_token_ids, json(logprobs) AS logprobs, "
    "json(metrics_extra) AS metrics_extra, json(extra) AS extra"
)
_TRAJ_COLS = (
    "trajectory_id, session_id, schema_version, agent_name, agent_version, "
    "model_name, total_prompt_tokens, total_completion_tokens, total_cached_tokens, "
    "total_cost_usd, total_steps, notes, continued_trajectory_ref, "
    "json(tool_definitions) AS tool_definitions, json(agent_extra) AS agent_extra, "
    "json(final_metrics_extra) AS final_metrics_extra, json(extra) AS extra"
)


def _build_step(conn: sqlite3.Connection, row: sqlite3.Row) -> Step:
    step_pk = row["id"]

    message: str | list[ContentPart]
    if row["message_parts"] is not None:
        message = [ContentPart.model_validate(p) for p in _load(row["message_parts"])]
    else:
        message = row["message"] if row["message"] is not None else ""

    tool_calls = [
        ToolCall(
            tool_call_id=tc["tool_call_id"],
            function_name=tc["function_name"],
            arguments=_load(tc["arguments"]) or {},
            extra=_load(tc["extra"]),
        )
        for tc in conn.execute(
            "SELECT tool_call_id, function_name, json(arguments) AS arguments, "
            "json(extra) AS extra FROM tool_calls WHERE step_pk = ? ORDER BY seq",
            (step_pk,),
        )
    ]

    obs_rows = list(
        conn.execute(
            "SELECT source_call_id, content, json(content_parts) AS content_parts, "
            "json(subagent_ref) AS subagent_ref, json(extra) AS extra "
            "FROM observation_results WHERE step_pk = ? ORDER BY seq",
            (step_pk,),
        )
    )
    observation: Observation | None = None
    if obs_rows:
        results = []
        for orow in obs_rows:
            if orow["content_parts"] is not None:
                content: Any = [ContentPart.model_validate(p) for p in _load(orow["content_parts"])]
            else:
                content = orow["content"]
            subagent_ref = None
            if orow["subagent_ref"] is not None:
                subagent_ref = [SubagentTrajectoryRef.model_validate(r) for r in _load(orow["subagent_ref"])]
            results.append(
                ObservationResult(
                    source_call_id=orow["source_call_id"],
                    content=content,
                    subagent_trajectory_ref=subagent_ref,
                    extra=_load(orow["extra"]),
                )
            )
        observation = Observation(results=results)

    is_copied = row["is_copied_context"]
    return Step(
        step_id=row["step_id"],
        timestamp=row["timestamp"],
        source=row["source"],
        model_name=row["model_name"],
        reasoning_effort=row["reasoning_effort"],
        message=message,
        reasoning_content=row["reasoning_content"],
        tool_calls=tool_calls or None,
        observation=observation,
        metrics=_build_metrics(row),
        llm_call_count=row["llm_call_count"],
        is_copied_context=None if is_copied is None else bool(is_copied),
        extra=_load(row["extra"]),
    )


def get(trajectory_id: str, db_path: Path | str = DEFAULT_DB_PATH) -> Trajectory | None:
    """Reassemble a stored trajectory into the ATIF ``Trajectory`` model.

    This is the granular-access path: the returned model carries every step,
    reasoning trace, tool call, observation, and metric -- reconstructed from
    the normalized rows, not from a stored blob, and validated by the ATIF
    pydantic models.
    """
    with connect(db_path) as conn:
        return _get(conn, trajectory_id)


def _get(conn: sqlite3.Connection, trajectory_id: str) -> Trajectory | None:
    trow = conn.execute(f"SELECT {_TRAJ_COLS} FROM trajectories WHERE trajectory_id = ?", (trajectory_id,)).fetchone()
    if trow is None:
        return None

    step_rows = conn.execute(
        f"SELECT {_STEP_COLS} FROM steps WHERE trajectory_id = ? ORDER BY step_id", (trajectory_id,)
    ).fetchall()
    steps = [_build_step(conn, srow) for srow in step_rows]

    fm = None
    fm_fields = {
        "total_prompt_tokens": trow["total_prompt_tokens"],
        "total_completion_tokens": trow["total_completion_tokens"],
        "total_cached_tokens": trow["total_cached_tokens"],
        "total_cost_usd": trow["total_cost_usd"],
        "total_steps": trow["total_steps"],
        "extra": _load(trow["final_metrics_extra"]),
    }
    if any(v is not None for v in fm_fields.values()):
        fm = FinalMetrics(**fm_fields)

    agent = Agent(
        name=trow["agent_name"],
        version=trow["agent_version"],
        model_name=trow["model_name"],
        tool_definitions=_load(trow["tool_definitions"]),
        extra=_load(trow["agent_extra"]),
    )

    # Re-nest embedded subagents in their original array order (sibling_seq).
    sub_ids = [
        r["trajectory_id"]
        for r in conn.execute(
            "SELECT trajectory_id FROM trajectories WHERE parent_trajectory_id = ? ORDER BY sibling_seq, trajectory_id",
            (trajectory_id,),
        )
    ]
    subagents = [_get(conn, sid) for sid in sub_ids]
    subagents = [s for s in subagents if s is not None]

    return Trajectory(
        schema_version=trow["schema_version"],
        session_id=trow["session_id"],
        trajectory_id=trow["trajectory_id"],
        agent=agent,
        steps=steps,
        notes=trow["notes"],
        final_metrics=fm,
        continued_trajectory_ref=trow["continued_trajectory_ref"],
        extra=_load(trow["extra"]),
        subagent_trajectories=subagents or None,
    )


# --- Query / stats ----------------------------------------------------------


def query(
    *,
    problem_id: str | None = None,
    agent: str | None = None,
    application: str | None = None,
    submitted: bool | None = None,
    include_subagents: bool = False,
    db_path: Path | str = DEFAULT_DB_PATH,
) -> list[TrajectorySummary]:
    """List trajectory summaries matching the filters (all optional).

    By default only root trajectories are listed (``parent_trajectory_id IS
    NULL``); set ``include_subagents`` to include embedded subagent rows.
    """
    clauses: list[str] = []
    params: list[Any] = []
    if not include_subagents:
        clauses.append("parent_trajectory_id IS NULL")
    if problem_id is not None:
        clauses.append("problem_id = ?")
        params.append(problem_id)
    if agent is not None:
        clauses.append("agent_name = ?")
        params.append(agent)
    if application is not None:
        clauses.append("application = ?")
        params.append(application)
    if submitted is not None:
        clauses.append("submitted = ?")
        params.append(int(submitted))

    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = (
        "SELECT trajectory_id, agent_name, problem_id, application, run, submitted, "
        "num_steps, total_cost_usd FROM trajectories" + where + " ORDER BY problem_id, agent_name, run"
    )
    with connect(db_path) as conn:
        rows = conn.execute(sql, params).fetchall()
    return [
        TrajectorySummary(
            trajectory_id=r["trajectory_id"],
            agent=r["agent_name"],
            problem_id=r["problem_id"],
            application=r["application"],
            run=r["run"],
            submitted=None if r["submitted"] is None else bool(r["submitted"]),
            num_steps=r["num_steps"],
            total_cost_usd=r["total_cost_usd"],
        )
        for r in rows
    ]


def stats(db_path: Path | str = DEFAULT_DB_PATH) -> dict[str, Any]:
    """Counts overall and grouped by problem type and agent (root trajectories)."""
    root = "WHERE parent_trajectory_id IS NULL"
    with connect(db_path) as conn:
        total = conn.execute(f"SELECT COUNT(*) AS n FROM trajectories {root}").fetchone()["n"]
        by_problem = {
            r["problem_id"]: r["n"]
            for r in conn.execute(
                f"SELECT problem_id, COUNT(*) AS n FROM trajectories {root} "
                "GROUP BY problem_id ORDER BY n DESC, problem_id"
            )
        }
        by_agent = {
            r["agent_name"]: r["n"]
            for r in conn.execute(
                f"SELECT agent_name, COUNT(*) AS n FROM trajectories {root} "
                "GROUP BY agent_name ORDER BY n DESC, agent_name"
            )
        }
    return {"total": total, "by_problem": by_problem, "by_agent": by_agent}


# --- CLI --------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m sregym.traces.store",
        description="Store and query normalized ATIF trajectories in SQLite.",
    )
    parser.add_argument("--db", type=Path, default=CLI_DEFAULT_DB_PATH, help="SQLite DB path.")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_ingest = sub.add_parser("ingest", help="Ingest trajectory.json files under a path.")
    p_ingest.add_argument("path", type=Path, help="A results/ tree or a single trajectory.json.")

    p_list = sub.add_parser("list", help="List stored trajectories.")
    p_list.add_argument("--problem", help="Filter by problem_id.")
    p_list.add_argument("--agent", help="Filter by agent name.")
    p_list.add_argument("--application", help="Filter by application display name.")
    p_list.add_argument("--submitted", choices=("true", "false"), help="Filter by submission success.")

    sub.add_parser("stats", help="Show counts by problem type and agent.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    if args.command == "ingest":
        if not args.path.exists():
            print(f"path does not exist: {args.path}", file=sys.stderr)
            return 2
        stored = ingest_tree(args.path, args.db)
        for tid in stored:
            print(tid)
        logger.info("Ingested %d trajectory(ies) into %s.", len(stored), args.db)
        return 0

    if args.command == "list":
        submitted = {"true": True, "false": False}.get(getattr(args, "submitted", None))
        summaries = query(
            problem_id=args.problem,
            agent=args.agent,
            application=args.application,
            submitted=submitted,
            db_path=args.db,
        )
        for s in summaries:
            cost = "" if s.total_cost_usd is None else f"${s.total_cost_usd:.4f}"
            print(
                f"{s.agent:<11} {str(s.problem_id):<40} run={s.run} "
                f"steps={s.num_steps:<4} submitted={s.submitted} {cost}\t{s.trajectory_id}"
            )
        logger.info("%d trajectory(ies).", len(summaries))
        return 0

    if args.command == "stats":
        s = stats(args.db)
        print(f"total: {s['total']}")
        print("by problem type:")
        for k, v in s["by_problem"].items():
            print(f"  {k}: {v}")
        print("by agent:")
        for k, v in s["by_agent"].items():
            print(f"  {k}: {v}")
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
