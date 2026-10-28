"""Opaque runtime artifacts and host-side semantic publication."""

import contextlib
import csv
import json
import os
import secrets
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sregym.service.container_runner import DEFAULT_AGENT_IMAGE


class ArtifactFinalizationError(RuntimeError):
    """An opaque artifact tree could not be safely published."""


@dataclass(frozen=True)
class RunArtifacts:
    artifact_id: str
    problem_id: str
    attempt: int
    staging_root: Path
    active_dir: Path
    final_dir: Path

    @property
    def network_audit_path(self) -> Path:
        # A sibling of the agent mount: the agent cannot edit this host record.
        return self.active_dir.with_name(f"{self.artifact_id}.internet-audit.json")

    def save_network_audit(self, audit: dict[str, Any]) -> None:
        """Retain the audit even if agent artifact validation later fails."""
        _write_json(self.network_audit_path, audit)

    @classmethod
    def create(
        cls,
        *,
        staging_root: Path,
        results_root: Path,
        problem_id: str,
        agent: str,
        attempt: int,
    ) -> "RunArtifacts":
        artifact_id = f"anon_{secrets.token_hex(16)}"
        active_dir = staging_root / agent / artifact_id
        staging_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        staging_root.chmod(0o700)
        active_dir.mkdir(parents=True, exist_ok=False)
        (active_dir / "trajectory").mkdir()
        return cls(
            artifact_id,
            problem_id,
            attempt,
            staging_root,
            active_dir,
            results_root / agent / problem_id / f"run_{attempt}",
        )

    def finalize_and_publish(
        self,
        *,
        snapshot: dict[str, Any],
        fieldnames: list[str],
        ownership_image: str = DEFAULT_AGENT_IMAGE,
    ) -> Path:
        """Run only after the evaluated runtime has been stopped and reaped."""
        self._assert_target_available()
        _normalize_ownership(self.active_dir, ownership_image)
        try:
            if hit := _find_token_hit(self.active_dir, self.problem_id):
                raise ArtifactFinalizationError(f"real problem id found in opaque artifacts: {hit}")

            paths = list(_walk(self.active_dir))
            for path in paths:
                if path.is_symlink() or not path.is_file():
                    continue
                if path.suffix == ".json":
                    _rewrite_json(path, self.artifact_id, self.problem_id)
                elif path.suffix == ".jsonl":
                    _rewrite_jsonl(path, self.artifact_id, self.problem_id)
                elif path.suffix == ".csv":
                    _rewrite_csv(path, self.artifact_id, self.problem_id)
            _rename_paths(paths, self.artifact_id, self.problem_id)

            attempt_csv = self.active_dir / f"{self.problem_id}_results.csv"
            _write_csv(attempt_csv, fieldnames, [snapshot])
            if _csv_problem_ids(attempt_csv) != [self.problem_id]:
                raise ArtifactFinalizationError(f"invalid per-attempt CSV: {attempt_csv}")
            if any(self.artifact_id in path.name for path in _walk(self.active_dir)):
                raise ArtifactFinalizationError("opaque artifact name remains after canonicalization")
            if self.network_audit_path.exists():
                # Add trusted metadata only after validating agent artifacts.
                # Atomic replacement does not follow an agent-created symlink.
                _write_json(
                    self.active_dir / "internet_audit.json",
                    json.loads(self.network_audit_path.read_text(encoding="utf-8")),
                )
        except ArtifactFinalizationError:
            raise
        except (OSError, csv.Error, ValueError) as exc:
            raise ArtifactFinalizationError(f"could not finalize opaque artifacts: {exc}") from exc

        self._assert_target_available()
        self.final_dir.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.active_dir.rename(self.final_dir)
        except OSError as exc:
            raise ArtifactFinalizationError(f"could not publish artifact directory: {exc}") from exc
        # A leftover host copy must not turn successful publication into failure.
        with contextlib.suppress(OSError):
            self.network_audit_path.unlink(missing_ok=True)
        _remove_empty_parents(self.active_dir.parent, self.staging_root.parent)
        return self.final_dir

    def _assert_target_available(self) -> None:
        if self.final_dir.exists():
            raise ArtifactFinalizationError(f"final run directory already exists: {self.final_dir}")


def _walk(root: Path) -> Iterator[Path]:
    """Walk without following symlinks."""
    pending = [root]
    while pending:
        with os.scandir(pending.pop()) as entries:
            children = sorted(entries, key=lambda entry: entry.name)
        for entry in children:
            path = Path(entry.path)
            yield path
            if entry.is_dir(follow_symlinks=False):
                pending.append(path)


def _tree_is_rewritable(root: Path) -> bool:
    try:
        directories = [root]
        for path in _walk(root):
            if path.is_symlink():
                os.readlink(path)
            elif path.is_dir():
                directories.append(path)
            elif path.is_file():
                with path.open("rb"):
                    pass
        for directory in directories:
            probe = directory / f".sregym-write-probe-{secrets.token_hex(4)}"
            try:
                probe.touch()
            finally:
                probe.unlink(missing_ok=True)
        return True
    except OSError:
        return False


def _normalize_ownership(root: Path, image: str) -> None:
    if _tree_is_rewritable(root):
        return
    command = (
        "set -eu; "
        "find -P /artifacts -type d -exec chmod u+rwx {} +; "
        "find -P /artifacts -type f -exec chmod u+rw {} +; "
        f"chown -hR {os.getuid()}:{os.getgid()} /artifacts"
    )
    docker_command = [
        "docker",
        "run",
        "--rm",
        "--network=none",
        "--entrypoint",
        "sh",
        "--user",
        "0:0",
        "-v",
        f"{root.resolve()}:/artifacts",
        image,
        "-c",
        command,
    ]
    try:
        result = subprocess.run(docker_command, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ArtifactFinalizationError(f"artifact ownership repair failed: {exc}") from exc
    if result.returncode != 0:
        raise ArtifactFinalizationError(f"artifact ownership repair failed: {(result.stderr or result.stdout).strip()}")
    if not _tree_is_rewritable(root):
        raise ArtifactFinalizationError("artifact tree is not rewritable after ownership repair")


def _find_token_hit(root: Path, token: str) -> str | None:
    encoded = token.encode()
    overlap = len(encoded) - 1
    for path in _walk(root):
        if token in path.name:
            return f"path:{path}"
        if path.is_symlink():
            if token in os.readlink(path):
                return f"symlink:{path}"
            continue
        if not path.is_file():
            continue
        with path.open("rb") as file:
            tail = b""
            for chunk in iter(lambda: file.read(1024 * 1024), b""):
                data = tail + chunk
                if encoded in data:
                    return f"file:{path}"
                tail = data[-overlap:] if overlap else b""
    return None


def _rewrite_json(path: Path, artifact_id: str, problem_id: str) -> None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        if "_results_" in path.name or path.name == "summary.json":
            raise ArtifactFinalizationError(f"malformed JSON artifact: {path}") from exc
        return
    if not isinstance(data, dict) or "problem_id" not in data:
        return
    if data["problem_id"] == artifact_id:
        data["problem_id"] = problem_id
        _write_json(path, data)
    elif data["problem_id"] != problem_id:
        raise ArtifactFinalizationError(f"non-semantic problem_id in JSON artifact: {path}")


def _rewrite_jsonl(path: Path, artifact_id: str, problem_id: str) -> None:
    tmp = _temp_path(path)
    changed = False
    try:
        with path.open("rb") as source, tmp.open("wb") as target:
            for line in source:
                try:
                    data = json.loads(line)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    target.write(line)
                    continue
                if isinstance(data, dict) and data.get("problem_id") == artifact_id:
                    data["problem_id"] = problem_id
                    target.write((json.dumps(data, ensure_ascii=False) + "\n").encode())
                    changed = True
                else:
                    if isinstance(data, dict) and "problem_id" in data and data["problem_id"] != problem_id:
                        raise ArtifactFinalizationError(f"non-semantic problem_id in JSONL artifact: {path}")
                    target.write(line)
        if changed:
            os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _rewrite_csv(path: Path, artifact_id: str, problem_id: str) -> None:
    try:
        with path.open(newline="", encoding="utf-8") as csvfile:
            reader = csv.DictReader(csvfile)
            fields, rows = reader.fieldnames, list(reader)
    except (UnicodeDecodeError, csv.Error):
        return
    if not fields or "problem_id" not in fields:
        return
    changed = False
    for row in rows:
        if row.get("problem_id") == artifact_id:
            row["problem_id"], changed = problem_id, True
        elif row.get("problem_id") != problem_id:
            raise ArtifactFinalizationError(f"non-semantic problem_id in CSV artifact: {path}")
    if changed:
        _write_csv(path, fields, rows)


def _rename_paths(paths: list[Path], artifact_id: str, problem_id: str) -> None:
    for path in sorted(paths, key=lambda item: len(item.parts), reverse=True):
        if artifact_id not in path.name:
            continue
        target = path.with_name(path.name.replace(artifact_id, problem_id))
        if target.exists() or target.is_symlink():
            raise ArtifactFinalizationError(f"canonical artifact path already exists: {target}")
        path.rename(target)


def _csv_problem_ids(path: Path) -> list[str]:
    with path.open(newline="", encoding="utf-8") as csvfile:
        reader = csv.DictReader(csvfile)
        return [row.get("problem_id", "") for row in reader] if reader.fieldnames else []


@dataclass(frozen=True)
class HeaderStabilizationResult:
    """rewritten: files whose header was widened to the run's union.
    overflow_dropped: files that had at least one row with more values than
    that file's own header -- a real bug somewhere upstream (something wrote
    a malformed line), not benign header drift, so a caller should surface
    it distinctly rather than folding it into a routine "normalized N
    files" count."""

    rewritten: list[Path]
    overflow_dropped: list[Path]


def stabilize_run_csv_headers(root: Path) -> HeaderStabilizationResult:
    """Give every per-attempt result CSV under root the same header.

    finalize_and_publish() writes each attempt's row against whatever
    fieldnames the driver had accumulated by that point in the run, so a
    file published early sees fewer columns than one published once some
    later attempt's oracle has contributed a key of its own.

    Call this once a run's attempts are all in. It rewrites every already-
    published per-attempt CSV to the union of every header seen under root,
    so a reader can trust the first line regardless of which file it opens
    first. A column a specific row never produced comes back empty
    (csv.DictWriter's own restval=""), distinct from a column that ran and
    recorded False -- the row for that column is simply absent from that
    file's original data, not written as "".

    Returns which files were actually rewritten (header was not already the
    union) and which had a ragged row's overflow dropped, so a caller can
    log each distinctly; a run with a uniform header, or fewer than two
    files, is left untouched.

    A file that cannot be read, decoded or parsed as CSV is skipped rather
    than aborting the whole pass: this is best-effort header normalization,
    not a correctness-critical step. A row with more values than its file's
    own header (a ragged row) has its unlabeled overflow dropped in the
    rewrite: DictWriter has no fieldname to write it under, and would
    otherwise raise. Such a file is always rewritten, so the report that
    its overflow was dropped is true even when its header already matched.

    Only the files finalize_and_publish writes are considered, at exactly
    <agent>/<problem>/run_<n>/<problem>_results.csv. The run directory also
    holds the agent's own artifacts, which may contain CSVs of any name and
    symlinks pointing anywhere; neither is followed.
    """
    paths = sorted(
        path
        for path in root.glob("*/*/run_*/*_results.csv")
        if path.name == f"{path.parent.parent.name}_results.csv" and not path.is_symlink() and path.is_file()
    )
    if len(paths) < 2:
        return HeaderStabilizationResult([], [])

    parsed: list[tuple[Path, list[str], list[dict[str, Any]]]] = []
    fields: set[str] = set()
    overflow_dropped: list[Path] = []
    for path in paths:
        try:
            with path.open(newline="", encoding="utf-8") as csvfile:
                reader = csv.DictReader(csvfile)
                file_fields = list(reader.fieldnames or [])
                raw_rows = list(reader)
        except (OSError, UnicodeDecodeError, csv.Error):
            continue
        ragged = any(None in row for row in raw_rows)
        if ragged:
            overflow_dropped.append(path)
        rows = [{k: v for k, v in row.items() if k is not None} for row in raw_rows]
        parsed.append((path, file_fields, rows, ragged))
        fields.update(file_fields)

    union = sorted(fields)
    rewritten = []
    for path, file_fields, rows, ragged in parsed:
        if file_fields != union or ragged:
            _write_csv(path, union, rows)
            rewritten.append(path)
    return HeaderStabilizationResult(rewritten, overflow_dropped)


def _write_csv(path: Path, fields: list[str], rows: list[dict[str, Any]]) -> None:
    tmp = _temp_path(path)
    try:
        with tmp.open("w", newline="", encoding="utf-8") as csvfile:
            writer = csv.DictWriter(csvfile, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _write_json(path: Path, data: dict[str, Any]) -> None:
    tmp = _temp_path(path)
    try:
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _temp_path(path: Path) -> Path:
    return path.with_name(f".{path.name}.sregym-{secrets.token_hex(4)}.tmp")


def _remove_empty_parents(path: Path, stop_at: Path) -> None:
    while path != stop_at and path != path.parent:
        try:
            path.rmdir()
        except OSError:
            break
        path = path.parent
