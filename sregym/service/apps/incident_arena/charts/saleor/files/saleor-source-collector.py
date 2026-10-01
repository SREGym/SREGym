"""Protected source snapshots for Saleor build-capable tasks.

Runs beside loadgen, outside the agent surface.  It materializes the exact
allow-listed source files into the standard verifier config trees.  The
declaration snapshot is immutable; the soak-end tree tracks later changes so
post-declaration drift remains visible.
"""

from __future__ import annotations

import json
import os
import stat
import time
from pathlib import Path, PurePosixPath
from typing import NoReturn

GRADER = Path(os.environ.get("GRADER_DIR", "/grader"))
SOURCE = Path(os.environ.get("SOURCE_ROOT", "/source"))
MAX_FILE_BYTES = int(os.environ.get("SOURCE_MAX_FILE_BYTES", "1048576"))
MAX_BYTES = int(os.environ.get("SOURCE_MAX_BYTES", "4194304"))


def fail(message: str) -> NoReturn:
    raise SystemExit(f"saleor-source-collector: FATAL: {message}")


def load_paths() -> list[str]:
    try:
        values = json.loads(os.environ["SOURCE_PATHS_JSON"])
    except (KeyError, json.JSONDecodeError) as exc:
        fail(f"SOURCE_PATHS_JSON is missing or malformed: {exc}")
    if not isinstance(values, list) or not values or len(values) > 8:
        fail("SOURCE_PATHS_JSON must contain one to eight paths")
    prefix = ("services", "app", "src", "saleor")
    out: list[str] = []
    for value in values:
        path = PurePosixPath(value) if isinstance(value, str) else None
        if (
            path is None
            or path.is_absolute()
            or value != path.as_posix()
            or path.parts[:4] != prefix
            or len(path.parts) <= 4
            or path.suffix != ".py"
        ):
            fail(f"invalid source path: {value!r}")
        out.append(value)
    if len(out) != len(set(out)):
        fail("source paths must be unique")
    return out


def read_source(logical: str) -> bytes:
    path = SOURCE / logical
    try:
        info = path.lstat()
    except OSError as exc:
        fail(f"cannot stat {logical}: {exc}")
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
        fail(f"source must be a regular non-symlink file: {logical}")
    if info.st_size > MAX_FILE_BYTES:
        fail(f"source file exceeds limit: {logical} ({info.st_size} bytes)")
    try:
        data = path.read_bytes()
    except OSError as exc:
        fail(f"cannot read {logical}: {exc}")
    if len(data) != info.st_size:
        fail(f"source changed while read: {logical}")
    return data


def snapshot(tree: str, paths: list[str]) -> None:
    payloads = [(logical, read_source(logical)) for logical in paths]
    total = sum(len(data) for _logical, data in payloads)
    if total > MAX_BYTES:
        fail(f"source snapshot exceeds total byte limit: {total}")
    for logical, data in payloads:
        target = GRADER / tree / logical
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.tmp")
        temporary.write_bytes(data)
        temporary.replace(target)


def write_manifest(paths: list[str], *, declared: bool) -> None:
    document: dict[str, list[str]] = {
        "config_before": paths,
        "config_after": paths,
    }
    if declared:
        document["config_after_soak_end"] = paths
    target = GRADER / "source_manifest.json"
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(document, indent=2, sort_keys=True))
    temporary.replace(target)


def repair_declared() -> bool:
    """Return whether the agent has declared its repair complete.

    The declaration snapshot is the one file only ``POST
    /declare_repair_complete`` writes, and it is written before LoadGen flips,
    so its existence is the in-episode declaration signal. This used to read
    ``report.json`` instead, back when filing an incident report was what ended
    the episode; the report is now advisory and an agent can declare without
    filing one, so keying off the report would miss real declarations.
    """
    return (GRADER / "config_at_submission.json").is_file()


def update_declaration_snapshots(paths: list[str], *, declared: bool) -> bool:
    """Advance the declaration/soak snapshots by one collector poll."""
    if declared:
        snapshot("config_after_soak_end", paths)
        return True
    if not repair_declared():
        return False
    snapshot("config_after", paths)
    snapshot("config_after_soak_end", paths)
    write_manifest(paths, declared=True)
    return True


def stay_alive_after_finalization() -> NoReturn:
    """Keep the loadgen Pod Ready while the verifier fetches final evidence.

    This collector is a regular container in the multi-container loadgen Pod.
    Returning would make the Pod NotReady and remove its Service endpoint even
    though the loadgen HTTP container is still serving the finalized bundle.
    The default SIGTERM/SIGINT handling remains intact for Kubernetes teardown.
    """
    while True:
        time.sleep(3600)


def main() -> None:
    paths = load_paths()
    deadline = time.monotonic() + 600
    while not (SOURCE / ".seeded").is_file():
        if time.monotonic() >= deadline:
            fail("task-local source was not seeded within 600 seconds")
        time.sleep(0.1)

    # Nop has identical before/after source. A declaration replaces only the
    # after tree once, then a separate tree tracks all later drift.
    snapshot("config_before", paths)
    snapshot("config_after", paths)
    write_manifest(paths, declared=False)
    declared = False
    while True:
        declared = update_declaration_snapshots(paths, declared=declared)
        if (GRADER / "episode_done.json").is_file():
            print("saleor-source-collector: finalized source evidence", flush=True)
            stay_alive_after_finalization()
        time.sleep(0.05)


if __name__ == "__main__":
    main()
