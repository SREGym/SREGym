"""SREGym run-directory wrapper for the standalone Gemini adapter."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from atif_converter import Trajectory
from atif_converter.adapters import gemini as standalone
from sregym.traces.adapters._compat import attach_sregym_meta


def _find_session_file(run_dir: Path) -> Path | None:
    sessions_root = run_dir / "sessions"
    if not sessions_root.exists():
        return None
    candidates = list(sessions_root.rglob("session-*.json")) + list(sessions_root.rglob("session-*.jsonl"))
    if not candidates:
        candidates = list(sessions_root.rglob("*.json")) + list(sessions_root.rglob("*.jsonl"))
    return max(candidates, key=lambda path: path.stat().st_mtime) if candidates else None


def to_atif(run_dir: Path | str, *, sregym_meta: dict[str, Any] | None = None) -> Trajectory | None:
    """Convert an archived SREGym Gemini run directory."""
    session_file = _find_session_file(Path(run_dir))
    if session_file is None:
        return None
    trajectory = standalone.convert_file(session_file)
    return attach_sregym_meta(trajectory, sregym_meta) if trajectory is not None else None
