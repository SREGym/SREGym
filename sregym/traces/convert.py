"""Conversion dispatch: raw run dir -> validated ATIF ``Trajectory``.

Detects the tool from the canonical results path, calls the matching adapter,
derives SREGym metadata (``extra.sregym``), and returns a validated trajectory.

Canonical run path layout (post-finalization, host-side):

    results/<batch>/<tool>/<problem_id>/run_<n>/
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sregym.traces.adapters import claudecode, codex, opencode
from sregym.traces.adapters import copilot as copilot_adapter
from sregym.traces.adapters import gemini as gemini_adapter
from sregym.traces.adapters import stratus as stratus_adapter
from sregym.traces.atif import Trajectory

logger = logging.getLogger(__name__)

# Tool name (as it appears in the results path) -> adapter ``to_atif`` callable.
ADAPTERS: dict[str, Callable[..., Trajectory | None]] = {
    "claudecode": claudecode.to_atif,
    "codex": codex.to_atif,
    "opencode": opencode.to_atif,
    "copilot": copilot_adapter.to_atif,
    "stratus": stratus_adapter.to_atif,
    "gemini": gemini_adapter.to_atif,
}

# Longest-suffix map of problem_id -> canonical application display name.
# Suffixes mirror the ``app_name=`` keys in
# ``sregym/conductor/problems/registry.py`` and the AppRegistry display names in
# ``sregym/service/apps/app_registry.py``. Ordered longest-first so the most
# specific suffix wins (e.g. ``blueprint_hotel_reservation`` before
# ``hotel_reservation``).
_APPLICATION_SUFFIXES: list[tuple[str, str]] = sorted(
    [
        ("blueprint_hotel_reservation", "Blueprint Hotel Reservation"),
        ("hotel_reservation", "Hotel Reservation"),
        ("social_network", "Social Network"),
        ("astronomy_shop", "Astronomy Shop"),
        ("train_ticket", "Train Ticket"),
        ("flight_ticket", "Flight Ticket"),
        ("fleet_cast", "Fleet Cast"),
    ],
    key=lambda kv: len(kv[0]),
    reverse=True,
)

# The conductor's success response to a submit (MCP ``submit`` tool or
# ``POST /submit``) is a small JSON object. The key is reported under either
# ``message`` or ``text`` depending on the path (see
# ``sregym/conductor/conductor_api.py``).
_SUBMISSION_MARKER = "Submission received"
_SUBMISSION_RESPONSE_KEYS = ("message", "text")

# Matches an embedded JSON object that looks like a conductor submit response,
# e.g. ``{"status":"200","message":"Submission received"}``. The observation
# content the adapter builds may wrap this with ``[stdout]`` / ``[metadata]``
# sections, so we search for the object rather than parse the whole blob.
_SUBMISSION_OBJ_RE = re.compile(r"\{[^{}]*\bSubmission received\b[^{}]*\}")


@dataclass(frozen=True)
class RunPathInfo:
    """Parsed components of a canonical run directory path."""

    batch: str
    tool: str
    problem_id: str
    run: int
    results_path: str


def parse_run_path(run_dir: Path | str) -> RunPathInfo:
    """Parse ``results/<batch>/<tool>/<problem_id>/run_<n>/`` into components.

    Raises:
        ValueError: if the path does not match the canonical layout.
    """
    run_dir = Path(run_dir)
    parts = run_dir.parts
    if len(parts) < 4:
        raise ValueError(f"run path too short to parse: {run_dir}")

    run_name = parts[-1]
    m = re.fullmatch(r"run_(\d+)", run_name)
    if not m:
        raise ValueError(f"expected a 'run_<n>' leaf, got {run_name!r} in {run_dir}")
    run = int(m.group(1))

    problem_id = parts[-2]
    tool = parts[-3]
    batch = parts[-4]
    results_path = str(Path(batch) / tool / problem_id / run_name)
    return RunPathInfo(
        batch=batch,
        tool=tool,
        problem_id=problem_id,
        run=run,
        results_path=results_path,
    )


def map_application(problem_id: str) -> str | None:
    """Map a ``problem_id`` to a canonical application name by longest suffix.

    Returns ``None`` when no known app suffix matches (e.g. ``kubelet_crash``,
    ``operator_*``), in which case ``application`` is omitted from metadata.
    """
    for suffix, name in _APPLICATION_SUFFIXES:
        if problem_id == suffix or problem_id.endswith("_" + suffix):
            return name
    return None


def _read_result_json(run_dir: Path) -> dict[str, Any] | None:
    """Read the per-run ``<tool>_results_<problem>_<ts>.json`` if present."""
    candidates = sorted(run_dir.glob("*_results_*.json"))
    for path in candidates:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict) and "success" in data:
            return data
    return None


def _is_submission_response(text: str) -> bool:
    """True if ``text`` contains a conductor submit-success response object.

    Requires the structured envelope (a JSON object whose ``message`` or
    ``text`` field equals ``"Submission received"``), not a bare substring, so
    that the phrase merely appearing in unrelated tool output (e.g. an agent
    grepping a prior log) does not mislabel the diagnosis -> mitigation
    boundary.
    """
    if _SUBMISSION_MARKER not in text:
        return False
    for match in _SUBMISSION_OBJ_RE.finditer(text):
        try:
            obj = json.loads(match.group(0))
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        if any(obj.get(key) == _SUBMISSION_MARKER for key in _SUBMISSION_RESPONSE_KEYS):
            return True
    return False


def _result_text(content: Any) -> str:
    """Flatten observation-result content to a single searchable string."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return json.dumps([getattr(p, "text", p) for p in content], default=str)
    return ""


def _find_diagnosis_submitted_step(trajectory: Trajectory) -> int | None:
    """Step id of the first step whose observation reports a successful submit.

    The conductor responds to a successful submit (MCP ``submit`` tool or
    ``POST /submit``) with a structured ``{"status": "200", "message":
    "Submission received"}`` envelope. The first such observation marks the
    diagnosis -> mitigation boundary.
    """
    for step in trajectory.steps:
        if not step.observation:
            continue
        for result in step.observation.results:
            if _is_submission_response(_result_text(result.content)):
                return step.step_id
    return None


def build_sregym_meta(run_dir: Path, info: RunPathInfo) -> dict[str, Any]:
    """Assemble the ``extra.sregym`` payload from the path + result JSON."""
    meta: dict[str, Any] = {
        "problem_id": info.problem_id,
        "run": info.run,
        "results_path": info.results_path,
    }
    application = map_application(info.problem_id)
    if application is not None:
        meta["application"] = application

    result = _read_result_json(run_dir)
    if result is not None:
        meta["submitted"] = bool(result.get("success"))

    return meta


def convert_run(run_dir: Path | str) -> Trajectory | None:
    """Convert one canonical run directory into a validated ATIF trajectory.

    Returns ``None`` if the tool is unknown or no convertible session exists.
    """
    run_dir = Path(run_dir)
    info = parse_run_path(run_dir)

    adapter = ADAPTERS.get(info.tool)
    if adapter is None:
        logger.debug("No ATIF adapter for tool %r (%s)", info.tool, run_dir)
        return None

    sregym_meta = build_sregym_meta(run_dir, info)

    trajectory = adapter(run_dir, sregym_meta=sregym_meta)
    if trajectory is None:
        return None

    # Boundary detection runs on the assembled trajectory; add it only when a
    # submission is found. Always (re)attach the assembled metadata so a run
    # with no submission still carries a complete, single-source extra.sregym.
    boundary = _find_diagnosis_submitted_step(trajectory)
    if boundary is not None:
        sregym_meta["diagnosis_submitted_step"] = boundary
    # Merge rather than overwrite so an adapter that enriches ``extra.sregym``
    # (e.g. stratus adds per-stage ``stages``) is not silently wiped. The
    # convert-level meta (path, application, result-JSON submitted) wins on key
    # collisions, but adapter-only keys are preserved.
    adapter_sregym = (trajectory.extra or {}).get("sregym", {})
    if isinstance(adapter_sregym, dict):
        sregym_meta = {**adapter_sregym, **sregym_meta}
    other_extra = {k: v for k, v in (trajectory.extra or {}).items() if k != "sregym"}
    merged = {**other_extra, "sregym": sregym_meta}
    trajectory.extra = merged or None

    # Per-document unique id (ATIF v1.7: optional on root but recommended for
    # deduplication). Use the canonical path so it's deterministic and stable.
    if not trajectory.trajectory_id:
        trajectory.trajectory_id = info.results_path

    return trajectory
