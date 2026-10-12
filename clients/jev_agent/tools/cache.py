"""The tool cache: a gzip JSONL record of tool results, replayable in recorded order."""

from __future__ import annotations

import copy
import gzip
import json
import threading
from pathlib import Path

from clients.jev_agent.types import ToolResult
from clients.jev_diag.timeutil import now


class ToolCache:
    """gzip JSONL record of tool results; with replay=True it serves only recorded results (in recorded order)."""

    def __init__(self, path: str | Path, replay: bool = False) -> None:
        """Open the cache file: load its recorded results for replay, or open it for appending."""
        self.path = Path(path)
        self.replay = replay
        self._lock = threading.Lock()
        self._entries: dict[tuple[str, str], list[ToolResult]] = {}
        self._handle = None
        if replay:
            if self.path.exists():
                with gzip.open(self.path, "rt", encoding="utf-8") as handle:
                    for line in handle:
                        if line.strip():
                            rec = json.loads(line)
                            self._entries.setdefault((rec["tool"], rec["target"]), []).append(_from_record(rec))
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._handle = gzip.open(self.path, "at", encoding="utf-8")  # noqa: SIM115 - closed in close()

    def lookup(self, name: str, target: str) -> ToolResult | None:
        """The cached result of a tool run, or None (replay serves recorded results in order)."""
        with self._lock:
            queue = self._entries.get((name, target))
            if not queue:
                return None
            # Replay serves results in recorded order (the last one repeats); live mode returns the latest.
            result = (queue.pop(0) if len(queue) > 1 else queue[0]) if self.replay else queue[-1]
            return ToolResult(lines=list(result.lines), error=result.error, raw=copy.deepcopy(result.raw))

    def record(self, name: str, target: str, result: ToolResult) -> None:
        """Append a tool result to the cache file (not in replay mode)."""
        if self.replay:
            return
        rec = {
            "tool": name,
            "target": target,
            "at": now().isoformat(),
            "lines": result.lines,
            "error": result.error,
            "raw": result.raw,
        }
        with self._lock:
            self._entries.setdefault((name, target), []).append(
                ToolResult(lines=list(result.lines), error=result.error, raw=result.raw)
            )
            if self._handle is not None:
                self._handle.write(json.dumps(rec, default=str) + "\n")
                self._handle.flush()

    def close(self) -> None:
        """Close the cache file."""
        with self._lock:
            if self._handle is not None:
                self._handle.close()
                self._handle = None


def _from_record(rec: dict) -> ToolResult:
    """A ToolResult from one cache file record."""
    return ToolResult(lines=list(rec.get("lines") or []), error=rec.get("error"), raw=rec.get("raw"))
