"""SREGym run-directory wrapper for the standalone Stratus adapter."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from atif_converter import Trajectory
from atif_converter.adapters import stratus as standalone


def _find_trajectory_file(run_dir: Path) -> Path | None:
    candidates = sorted(run_dir.glob("*_stratus_agent_trajectory.jsonl"))
    return max(candidates, key=lambda path: (path.name, path.stat().st_mtime)) if candidates else None


def to_atif(run_dir: Path | str, *, sregym_meta: dict[str, Any] | None = None) -> Trajectory | None:
    """Convert an archived SREGym Stratus run directory."""
    session_file = _find_trajectory_file(Path(run_dir))
    if session_file is None:
        return None
    trajectory = standalone.convert_file(session_file)
    if trajectory is None:
        return None

    extra = dict(trajectory.extra or {})
    stratus_meta = extra.pop("stratus", {})
    sregym = dict(sregym_meta or {})
    if isinstance(stratus_meta, dict):
        stages = stratus_meta.get("stages")
        if stages is not None:
            sregym["stages"] = stages
        if "submitted" in stratus_meta:
            sregym.setdefault("submitted", stratus_meta["submitted"])
        if "diagnosis_submitted_step" in stratus_meta:
            sregym.setdefault("diagnosis_submitted_step", stratus_meta["diagnosis_submitted_step"])
    extra["sregym"] = sregym
    trajectory.extra = extra
    return trajectory
