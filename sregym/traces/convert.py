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
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from atif_converter import SUPPORTED_AGENTS, AtifConverterError, Trajectory
from atif_converter import convert as convert_session
from atif_converter.adapters import claudecode

logger = logging.getLogger(__name__)

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


def _find_claudecode_session_files(run_dir: Path) -> list[Path]:
    """Find all top-level JSONL fragments for one archived Claude session."""
    project_root = run_dir / "sessions" / "projects"
    if not project_root.is_dir():
        return []

    session_dirs: list[Path] = []
    for project_dir in project_root.iterdir():
        if project_dir.is_dir():
            jsonl_files = list(project_dir.rglob("*.jsonl"))
            session_dirs.extend({path.parent for path in jsonl_files if "subagents" not in path.parent.parts})
    if len(session_dirs) != 1:
        if session_dirs:
            logger.debug("Expected one Claude Code session directory in %s, found %d", run_dir, len(session_dirs))
        return []
    return list(session_dirs[0].glob("*.jsonl"))


def _claudecode_total_cost_usd(run_dir: Path) -> float | None:
    """Read Claude's authoritative cost from its separate stream output."""
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


def _find_codex_session_file(run_dir: Path) -> Path | None:
    sessions_root = run_dir / "sessions"
    if not sessions_root.exists():
        return None
    session_dirs = [path for path in sessions_root.rglob("*") if path.is_dir()]
    if not session_dirs:
        return None
    max_depth = max(len(path.parts) for path in session_dirs)
    deepest = [path for path in session_dirs if len(path.parts) == max_depth]
    if len(deepest) != 1:
        return None
    files = list(deepest[0].glob("*.jsonl"))
    return files[0] if files else None


def _find_gemini_session_file(run_dir: Path) -> Path | None:
    sessions_root = run_dir / "sessions"
    if not sessions_root.exists():
        return None
    candidates = list(sessions_root.rglob("session-*.json")) + list(sessions_root.rglob("session-*.jsonl"))
    if not candidates:
        candidates = list(sessions_root.rglob("*.json")) + list(sessions_root.rglob("*.jsonl"))
    return max(candidates, key=lambda path: path.stat().st_mtime) if candidates else None


def _find_session_file(run_dir: Path, tool: str) -> Path | None:
    """Resolve a canonical SREGym run directory to its native session file."""
    if tool == "codex":
        return _find_codex_session_file(run_dir)
    if tool == "copilot":
        path = run_dir / "copilot-cli.jsonl"
        return path if path.is_file() else None
    if tool == "gemini":
        return _find_gemini_session_file(run_dir)
    if tool == "opencode":
        candidates = sorted((run_dir / "sessions").rglob("session-*.json"))
        return candidates[0] if candidates else None
    if tool == "stratus":
        candidates = sorted(run_dir.glob("*_stratus_agent_trajectory.jsonl"))
        return max(candidates, key=lambda path: (path.name, path.stat().st_mtime)) if candidates else None
    return None


def _convert_native_run(run_dir: Path, tool: str) -> Trajectory | None:
    """Convert the native artifact selected from one canonical SREGym run."""
    try:
        if tool == "claudecode":
            session_files = _find_claudecode_session_files(run_dir)
            if not session_files:
                return None
            return claudecode.convert_files(
                session_files,
                total_cost_usd=_claudecode_total_cost_usd(run_dir),
            )

        session_file = _find_session_file(run_dir, tool)
        if session_file is None:
            return None
        return convert_session(session_file, agent=tool)
    except AtifConverterError as exc:
        logger.debug("Could not convert %s run %s: %s", tool, run_dir, exc)
        return None
    except Exception as exc:
        # Claude's multi-file API is intentionally lower-level than the public
        # convert() dispatcher, so normalize any unexpected adapter failure to
        # SREGym's established non-fatal conversion behavior here.
        logger.debug("Could not convert %s run %s: %s", tool, run_dir, exc, exc_info=True)
        return None


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

    if info.tool not in SUPPORTED_AGENTS:
        logger.debug("No ATIF adapter for tool %r (%s)", info.tool, run_dir)
        return None

    sregym_meta = build_sregym_meta(run_dir, info)

    trajectory = _convert_native_run(run_dir, info.tool)
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
    other_extra = dict(trajectory.extra or {})
    adapter_sregym = other_extra.pop("sregym", {})
    if isinstance(adapter_sregym, dict):
        sregym_meta = {**adapter_sregym, **sregym_meta}

    # The standalone Stratus adapter exposes neutral stage metadata. SREGym
    # owns the domain-specific namespace, so fold it into extra.sregym here.
    stratus_meta = other_extra.pop("stratus", None)
    if isinstance(stratus_meta, dict):
        sregym_meta = {**stratus_meta, **sregym_meta}
    merged = {**other_extra, "sregym": sregym_meta}
    trajectory.extra = merged or None

    # Per-document unique id (ATIF v1.7: optional on root but recommended for
    # deduplication). Use the canonical path so it's deterministic and stable.
    if not trajectory.trajectory_id:
        trajectory.trajectory_id = info.results_path

    return trajectory
