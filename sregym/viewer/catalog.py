"""Discover ATIF files, validate selected traces, and join per-attempt results."""

import csv
import json
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock
from typing import Any

from pydantic import ValidationError

from atif_converter.atif.trajectory import Trajectory

MAX_FILE_BYTES = 64 * 1024 * 1024


@dataclass
class Record:
    path: Path
    data: dict[str, Any] | None = None
    trajectory: Trajectory | None = None
    error: str | None = None


@dataclass
class Run:
    key: str
    name: str
    agent: str = "Unknown"
    model: str = "Unknown"
    attempt: str = ""
    status: str = "Unknown"
    diagnosis: str = "Unknown"
    mitigation: str = "Unknown"
    evaluation: dict[str, str] = field(default_factory=dict)
    evaluation_note: str = "No per-attempt evaluation result was found."
    error: str | None = None
    campaign: str = "."
    application: str = ""
    steps: int = 0
    started_at: str = ""


def campaign_path(key: str, problem: str, agent: str) -> str:
    """Group by the actual directory above the agent/problem/attempt layout."""
    parts = Path(key).parts[:-1]
    if problem in parts:
        prefix = list(parts[: len(parts) - 1 - parts[::-1].index(problem)])
    else:
        prefix = list(parts[:-2] if parts and parts[-1].startswith("run_") else parts[:-1])
    if prefix and prefix[-1] == agent:
        prefix.pop()
    return "/".join(prefix) or "."


def grade(value: str | None) -> str:
    normalized = (value or "").strip().lower()
    return {"true": "Pass", "1": "Pass", "false": "Fail", "0": "Fail"}.get(normalized, "Unknown")


def sregym_metadata(trajectory: Trajectory) -> dict[str, Any]:
    value = (trajectory.extra or {}).get("sregym", {})
    return value if isinstance(value, dict) else {}


def read_evaluation(path: Path, problem: str, attempt: str, root: Path) -> tuple[dict[str, str], str]:
    """Use only results next to this trajectory, matched to the recorded attempt."""
    if not problem or not attempt:
        return {}, "This trace has no SREGym attempt identity. Evaluation grades remain unknown."
    candidates = sorted(path.parent.glob("*_results.csv"))
    exact = path.parent / f"{problem}_results.csv"
    if problem and exact in candidates:
        candidates = [exact]
    matches = []
    try:
        for candidate in candidates:
            if not candidate.resolve().is_relative_to(root) or candidate.stat().st_size > MAX_FILE_BYTES:
                continue
            with candidate.open(newline="", encoding="utf-8-sig") as stream:
                for row in csv.DictReader(stream):
                    if problem and row.get("problem_id") != problem:
                        continue
                    if attempt and row.get("attempt") != attempt:
                        continue
                    matches.append(row)
    except (OSError, UnicodeError, csv.Error) as exc:
        return {}, f"The evaluation file could not be read: {exc}"
    if len(matches) == 1:
        return matches[0], "Recorded per-attempt evaluation."
    if matches:
        return {}, "Multiple evaluation rows match this attempt. Grades remain unknown."
    return {}, "No matching per-attempt evaluation result was found."


class Catalog:
    """Bounded trace cache and refreshable file catalog. Paths never leave the selected root."""

    def __init__(self, source: Path):
        source = source.expanduser().resolve()
        self.root = source if source.is_dir() else source.parent
        self.single_file = source if source.is_file() else None
        self._allowed: set[str] = set()
        self._references: set[str] = set()
        self._documents: OrderedDict[str, tuple[tuple, Record]] = OrderedDict()
        self._summaries: dict[str, tuple[tuple, Run]] = {}
        self._lock = RLock()

    def path(self, key: str) -> Path:
        if key not in self._allowed:
            raise FileNotFoundError("The trajectory is not in this catalog.")
        path = (self.root / key).resolve()
        if not path.is_relative_to(self.root) or not path.is_file():
            raise FileNotFoundError("The file is outside the selected directory or no longer exists.")
        return path

    @staticmethod
    def _signature(path: Path) -> tuple:
        stat = path.stat()
        return stat.st_mtime_ns, stat.st_size, stat.st_ino

    def load(self, key: str) -> Record:
        with self._lock:
            path = self.path(key)
            signature = self._signature(path)
            cached = self._documents.get(key)
            if cached and cached[0] == signature:
                self._documents.move_to_end(key)
                return cached[1]
            record = Record(path)
            try:
                if signature[1] > MAX_FILE_BYTES:
                    raise ValueError("This file exceeds the 64 MiB viewer limit. Download it for external inspection.")
                data = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    raise ValueError("An ATIF trajectory must be a JSON object.")
                record.data = data
                record.trajectory = Trajectory.model_validate(data)
            except (OSError, UnicodeError, ValueError, RecursionError) as exc:
                if isinstance(exc, ValidationError):
                    errors = exc.errors(include_input=False, include_url=False)
                    record.error = "ATIF validation failed: " + json.dumps(errors[:5], ensure_ascii=False, default=str)
                else:
                    record.error = str(exc)
            # External references must be valid ATIF, not an arbitrary JSON file.
            if key in self._references and not record.trajectory:
                raise FileNotFoundError("The referenced file is not a supported ATIF trajectory.")
            self._documents[key] = signature, record
            while len(self._documents) > 4:
                self._documents.popitem(last=False)
            while len(self._documents) > 1 and sum(item[0][1] for item in self._documents.values()) > MAX_FILE_BYTES:
                self._documents.popitem(last=False)
            return record

    def runs(self) -> list[Run]:
        with self._lock:
            paths = [self.single_file] if self.single_file else sorted(self.root.rglob("trajectory.json"))
            keys = []
            for path in paths:
                resolved = path.resolve()
                if resolved.is_relative_to(self.root) and resolved.is_file():
                    key = path.relative_to(self.root).as_posix()
                    self._allowed.add(key)
                    keys.append(key)
            self._allowed.intersection_update(set(keys) | self._references)
            for stale in self._summaries.keys() - set(keys):
                del self._summaries[stale]
            result = []
            for key in keys:
                try:
                    path = self.path(key)
                    signature = (
                        self._signature(path),
                        tuple((p.name, self._signature(p)) for p in sorted(path.parent.glob("*_results.csv"))),
                    )
                    cached = self._summaries.get(key)
                    if cached and cached[0] == signature:
                        result.append(cached[1])
                        continue
                    record = self.load(key)
                    run = Run(key, path.parent.name if path.name == "trajectory.json" else path.name)
                    if record.trajectory:
                        trajectory = record.trajectory
                        metadata = sregym_metadata(trajectory)
                        problem = str(metadata.get("problem_id") or "")
                        attempt = str(metadata.get("run") or "")
                        run.name = problem or run.name
                        run.attempt = attempt
                        run.agent = trajectory.agent.name
                        run.model = trajectory.agent.model_name or "Unknown"
                        run.campaign = campaign_path(key, problem, run.agent)
                        run.application = str(metadata.get("application") or "")
                        run.steps = len(trajectory.steps)
                        run.started_at = str(next((s.timestamp for s in trajectory.steps if s.timestamp), ""))
                        run.evaluation, run.evaluation_note = read_evaluation(path, problem, attempt, self.root)
                        run.diagnosis = grade(run.evaluation.get("Diagnosis.success"))
                        run.mitigation = grade(run.evaluation.get("Mitigation.success"))
                        run.status = run.evaluation.get("run_status") or "Unknown"
                    else:
                        run.error = record.error
                        run.status = "Invalid trace"
                        run.campaign = campaign_path(key, "", run.agent)
                    self._summaries[key] = signature, run
                    result.append(run)
                except (OSError, ValueError):
                    # A run can disappear while the user refreshes a campaign directory.
                    continue
            return result

    def reference(self, owner: Path, value: str) -> str | None:
        """Register only supported local ATIF references. Never fetch remote content."""
        if "://" in value:
            return None
        with self._lock:
            path = (owner.parent / value).resolve()
            if not path.is_relative_to(self.root) or not path.is_file():
                return None
            key = path.relative_to(self.root).as_posix()
            was_allowed = key in self._allowed
            self._allowed.add(key)
            if not was_allowed:
                self._references.add(key)
            try:
                if not self.load(key).trajectory:
                    return None
            except (OSError, ValueError):
                if not was_allowed:
                    self._allowed.discard(key)
                    self._references.discard(key)
                return None
            return key
