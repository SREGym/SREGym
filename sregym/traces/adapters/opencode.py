"""SREGym run-directory wrapper for the standalone OpenCode adapter."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from atif_converter import Trajectory
from atif_converter.adapters import opencode as standalone
from sregym.traces.adapters._compat import attach_sregym_meta


def to_atif(run_dir: Path | str, *, sregym_meta: dict[str, Any] | None = None) -> Trajectory | None:
    """Convert an archived SREGym OpenCode run directory."""
    session_files = sorted((Path(run_dir) / "sessions").rglob("session-*.json"))
    if not session_files:
        return None
    trajectory = standalone.convert_file(session_files[0])
    return attach_sregym_meta(trajectory, sregym_meta) if trajectory is not None else None
