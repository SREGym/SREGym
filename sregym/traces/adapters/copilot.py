"""SREGym run-directory wrapper for the standalone Copilot adapter."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from atif_converter import Trajectory
from atif_converter.adapters import copilot as standalone
from sregym.traces.adapters._compat import attach_sregym_meta


def to_atif(run_dir: Path | str, *, sregym_meta: dict[str, Any] | None = None) -> Trajectory | None:
    """Convert an archived SREGym Copilot run directory."""
    session_file = Path(run_dir) / "copilot-cli.jsonl"
    if not session_file.is_file():
        return None
    trajectory = standalone.convert_file(session_file)
    return attach_sregym_meta(trajectory, sregym_meta) if trajectory is not None else None
