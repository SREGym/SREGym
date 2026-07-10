"""SREGym run-directory wrapper for the standalone Claude Code adapter."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from atif_converter import Trajectory
from atif_converter.adapters import claudecode as standalone
from sregym.traces.adapters._compat import attach_sregym_meta

logger = logging.getLogger(__name__)


def _get_session_dir(run_dir: Path) -> Path | None:
    sessions_root = run_dir / "sessions"
    project_root = sessions_root / "projects"
    if not project_root.is_dir():
        return None

    session_dirs: list[Path] = []
    for project_dir in project_root.iterdir():
        if project_dir.is_dir():
            jsonl_files = list(project_dir.rglob("*.jsonl"))
            session_dirs.extend({path.parent for path in jsonl_files if "subagents" not in path.parent.parts})
    if len(session_dirs) == 1:
        return session_dirs[0]
    if session_dirs:
        logger.debug("Multiple Claude Code session directories found in %s", run_dir)
    return None


def _total_cost_usd(run_dir: Path) -> float | None:
    try:
        lines = (run_dir / "claude-code.txt").read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        if not line.strip().startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") != "result" or event.get("total_cost_usd") is None:
            continue
        try:
            return float(event["total_cost_usd"])
        except (TypeError, ValueError):
            return None
    return None


def to_atif(run_dir: Path | str, *, sregym_meta: dict[str, Any] | None = None) -> Trajectory | None:
    """Convert an archived SREGym Claude Code run directory."""
    run_dir = Path(run_dir)
    session_dir = _get_session_dir(run_dir)
    if session_dir is None:
        return None
    trajectory = standalone.convert_files(
        list(session_dir.glob("*.jsonl")),
        total_cost_usd=_total_cost_usd(run_dir),
    )
    return attach_sregym_meta(trajectory, sregym_meta) if trajectory is not None else None
