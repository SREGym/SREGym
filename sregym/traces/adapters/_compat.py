"""Shared metadata helpers for SREGym adapter wrappers."""

from __future__ import annotations

from typing import Any

from atif_converter import Trajectory


def attach_sregym_meta(trajectory: Trajectory, sregym_meta: dict[str, Any] | None) -> Trajectory:
    """Attach optional SREGym metadata without discarding other extras."""
    if not sregym_meta:
        return trajectory
    extra = dict(trajectory.extra or {})
    previous = extra.get("sregym")
    merged = dict(previous) if isinstance(previous, dict) else {}
    merged.update(sregym_meta)
    extra["sregym"] = merged
    trajectory.extra = extra
    return trajectory
