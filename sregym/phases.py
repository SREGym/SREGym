"""Durable phase-boundary ledger for a run.

Every phase writes two records -- one on entry, one on exit -- appended and
fsynced as they happen. Nothing is buffered until the end of the run, because
the run ending is precisely the thing that cannot be relied upon: an agent
timeout, a `kill`, or a failed deploy all end a run without a tidy exit, and
those are the runs whose timings you most want. A missing `end` record then
means "died in this phase", which is information, rather than "we never got to
write anything down", which is not.

Two clocks are recorded at every boundary:

``wall``
    ``time.time()``. Comparable with log timestamps and across processes, and
    the axis you line phases up on. Not trustworthy as a duration: if the
    machine suspends mid-phase, wall time keeps advancing while nothing runs.

``mono``
    ``time.monotonic()``. Trustworthy as a duration, meaningless as an instant,
    and not comparable across processes.

Keeping both makes a suspend *detectable* rather than silently corrupting the
numbers -- see :func:`suspended_seconds`. This is not hypothetical: a run in
development recorded 80 minutes wall for what was a few minutes of work.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger("all.sregym.phases")

LEDGER_FILENAME = "phases.jsonl"

# Tolerance before wall/monotonic divergence is called a suspend rather than
# scheduling noise or clock adjustment.
SUSPEND_TOLERANCE_SECONDS = 5.0


def _utc_iso(wall: float) -> str:
    return datetime.fromtimestamp(wall, tz=UTC).isoformat().replace("+00:00", "Z")


class PhaseLedger:
    """Append-only record of phase boundaries for one run.

    Deliberately tolerant: a ledger that cannot be written must never be the
    reason a run fails, so IO errors are logged and swallowed.
    """

    def __init__(self, path: Path | str, *, context: dict | None = None):
        self.path = Path(path)
        self.context = dict(context or {})
        # Start times of phases opened but not yet closed, so a phase recorded
        # via two separate `record` calls still gets a duration. Without this,
        # only the context manager could produce one, and the caller would be
        # left subtracting timestamps -- the synthesis this module exists to
        # avoid.
        self._open: dict[str, tuple[float, float]] = {}
        self._enabled = True
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning(f"Phase ledger disabled; cannot create {self.path.parent}: {exc}")
            self._enabled = False

    def _append(self, record: dict) -> None:
        if not self._enabled:
            return
        try:
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, default=str) + "\n")
                fh.flush()
                # Survive a SIGKILL of this process, which is how an
                # agent-timeout run ends.
                os.fsync(fh.fileno())
        except OSError as exc:
            logger.warning(f"Phase ledger write failed ({record.get('phase')}): {exc}")

    def is_open(self, phase: str) -> bool:
        """True if `phase` has been started and not yet ended.

        The ledger is the authority on this. A caller that closes a phase
        defensively -- e.g. teardown closing a stage the agent may have
        abandoned -- must ask, or it writes a second end for a phase that
        already closed cleanly.
        """
        return phase in self._open

    def record(self, phase: str, event: str, **fields) -> None:
        """Append one boundary record, timing it against a matching start.

        `setdefault` throughout: the context manager computes its own durations
        and passes them in, and those win. This path only fills the gap for
        phases opened and closed from two different call sites.
        """
        wall, mono = time.time(), time.monotonic()

        if event == "start":
            self._open[phase] = (wall, mono)
        elif event == "end":
            opened = self._open.pop(phase, None)
            if opened is not None:
                start_wall, start_mono = opened
                elapsed = mono - start_mono
                fields.setdefault("duration_s", round(elapsed, 3))
                fields.setdefault("wall_duration_s", round(wall - start_wall, 3))
                suspended = (wall - start_wall) - elapsed
                if suspended > SUSPEND_TOLERANCE_SECONDS:
                    fields.setdefault("suspended_s", round(suspended, 3))

        self._append(
            {
                "phase": phase,
                "event": event,
                "wall": wall,
                "ts": _utc_iso(wall),
                "mono": mono,
                **self.context,
                **fields,
            }
        )

    @contextmanager
    def phase(self, name: str, **fields) -> Iterator[None]:
        """Record entry and exit around a block, whatever happens inside.

        The exit record is written in `finally`, so a raising phase still gets
        an end time and an `outcome` naming the exception. The exception then
        propagates unchanged -- this observes, it does not handle.
        """
        self.record(name, "start", **fields)
        start_wall, start_mono = time.time(), time.monotonic()
        outcome, error = "ok", None
        try:
            yield
        except BaseException as exc:  # noqa: BLE001 - re-raised below
            outcome = "error"
            error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            end_wall, end_mono = time.time(), time.monotonic()
            elapsed = end_mono - start_mono
            record: dict = {
                "duration_s": round(elapsed, 3),
                "wall_duration_s": round(end_wall - start_wall, 3),
                "outcome": outcome,
            }
            if error is not None:
                record["error"] = error
            suspended = (end_wall - start_wall) - elapsed
            if suspended > SUSPEND_TOLERANCE_SECONDS:
                # Wall ran ahead of monotonic: the host was not executing for
                # the difference. Flagged so the phase is excluded from
                # comparisons rather than quietly skewing them.
                record["suspended_s"] = round(suspended, 3)
            self.record(name, "end", **record)


def read_ledger(path: Path | str) -> list[dict]:
    """Parse a ledger, skipping any truncated trailing line.

    A partial final line is expected rather than exceptional: the process can
    die mid-write.
    """
    records = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    logger.debug(f"Ignoring unparseable ledger line in {path}")
    except OSError as exc:
        logger.warning(f"Could not read phase ledger {path}: {exc}")
    return records


def summarize(records: list[dict]) -> dict[str, dict]:
    """Pair start/end records into per-phase timings.

    A phase with a start and no end is reported with ``outcome`` `"incomplete"`
    and no duration -- the run died inside it. Phases that occur more than once
    are suffixed `#2`, `#3`, ... so a retried deploy stays distinguishable.
    """
    summary: dict[str, dict] = {}
    open_phases: dict[str, dict] = {}
    seen: dict[str, int] = {}

    for rec in records:
        name, event = rec.get("phase"), rec.get("event")
        if not name:
            continue
        if event == "start":
            seen[name] = seen.get(name, 0) + 1
            key = name if seen[name] == 1 else f"{name}#{seen[name]}"
            entry = {"start_ts": rec.get("ts"), "start_wall": rec.get("wall"), "outcome": "incomplete"}
            summary[key] = entry
            open_phases[name] = entry
        elif event == "end":
            entry = open_phases.pop(name, None)
            if entry is None:
                # An end with no start: keep it rather than dropping data, but
                # seed the start keys so every entry has the same shape. A
                # consumer indexing entry["start_ts"] must not blow up on the
                # one record that is missing it.
                seen[name] = seen.get(name, 0) + 1
                key = name if seen[name] == 1 else f"{name}#{seen[name]}"
                entry = summary.setdefault(key, {"start_ts": None, "start_wall": None})
            entry.update(
                {
                    "end_ts": rec.get("ts"),
                    "end_wall": rec.get("wall"),
                    "duration_s": rec.get("duration_s"),
                    "wall_duration_s": rec.get("wall_duration_s"),
                    "outcome": rec.get("outcome", "ok"),
                }
            )
            for optional in ("error", "suspended_s"):
                if optional in rec:
                    entry[optional] = rec[optional]
    return summary


def suspended_seconds(records: list[dict]) -> float:
    """Total time the host was not executing, across all phases."""
    return round(sum(r.get("suspended_s", 0.0) for r in records if r.get("event") == "end"), 3)


def results_columns(records: list[dict], *, prefix: str = "phase") -> dict[str, object]:
    """Flatten a ledger into columns for the results CSV.

    Emits `<prefix>.<name>.duration_s` and, only where interesting, `.outcome`
    -- so a clean run stays narrow and a broken one says where it broke.
    """
    columns: dict[str, object] = {}
    for name, entry in summarize(records).items():
        if entry.get("duration_s") is not None:
            columns[f"{prefix}.{name}.duration_s"] = entry["duration_s"]
        if entry.get("outcome") not in (None, "ok"):
            columns[f"{prefix}.{name}.outcome"] = entry.get("outcome")
    total_suspended = suspended_seconds(records)
    if total_suspended:
        columns[f"{prefix}.suspended_s"] = total_suspended
    return columns
