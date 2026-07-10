"""SREGym run-directory wrapper for the standalone Codex adapter."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from atif_converter import Trajectory
from atif_converter.adapters import codex as standalone
from sregym.traces.adapters._compat import attach_sregym_meta

logger = logging.getLogger(__name__)


def _get_session_dir(run_dir: Path) -> Path | None:
    sessions_root = run_dir / "sessions"
    if not sessions_root.exists():
        return None
    session_dirs = [path for path in sessions_root.rglob("*") if path.is_dir()]
    if not session_dirs:
        return None
    max_depth = max(len(path.parts) for path in session_dirs)
    deepest = [path for path in session_dirs if len(path.parts) == max_depth]
    if len(deepest) != 1:
        logger.debug("Expected exactly one Codex session directory in %s", run_dir)
        return None
    return deepest[0]


def to_atif(run_dir: Path | str, *, sregym_meta: dict[str, Any] | None = None) -> Trajectory | None:
    """Convert an archived SREGym Codex run directory."""
    session_dir = _get_session_dir(Path(run_dir))
    if session_dir is None:
        return None
    session_files = list(session_dir.glob("*.jsonl"))
    if not session_files:
        return None
    trajectory = standalone.convert_file(session_files[0])
    return attach_sregym_meta(trajectory, sregym_meta) if trajectory is not None else None
