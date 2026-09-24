"""Host-side postprocess: convert raw run logs to ATIF ``trajectory.json``.

Walks a ``results/`` tree (or a single ``run_<n>`` directory), converts each run
to a validated ATIF trajectory, and writes ``<run_dir>/trajectory.json``.
Idempotent: re-running overwrites with byte-identical output.

Must run host-side, AFTER ``RunArtifacts.finalize_and_publish`` (which restores
the real ``problem_id`` into the canonical path). Conversion failures and
unconvertible runs are skipped, never fatal.

Usage::

    python -m sregym.traces.postprocess results/                          # whole tree
    python -m sregym.traces.postprocess results/<batch>/<tool>/<p>/run_1  # one run
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from pathlib import Path

from sregym.traces import convert

logger = logging.getLogger(__name__)

_RUN_DIR_RE = re.compile(r"run_\d+")
OUTPUT_FILENAME = "trajectory.json"


def _is_run_dir(path: Path) -> bool:
    return path.is_dir() and bool(_RUN_DIR_RE.fullmatch(path.name))


def _iter_run_dirs(root: Path):
    """Yield run dirs under ``root`` (or ``root`` itself if it is a run dir)."""
    if _is_run_dir(root):
        yield root
        return
    for path in sorted(root.rglob("run_*")):
        if _is_run_dir(path):
            yield path


def write_trajectory(run_dir: Path) -> Path | None:
    """Convert one run dir and write ``trajectory.json``; return the path or None.

    Safe to call from the run pipeline: never raises on conversion/IO failure,
    and returns ``None`` for unknown tools or runs without a convertible session.
    """
    try:
        trajectory = convert.convert_run(run_dir)
    except ValueError as exc:
        logger.debug("Skipping %s: %s", run_dir, exc)
        return None
    except Exception as exc:  # defensive: never let one run abort the walk
        logger.warning("Failed to convert %s: %s", run_dir, exc)
        return None

    if trajectory is None:
        logger.warning("No convertible session in %s", run_dir)
        return None

    out_path = run_dir / OUTPUT_FILENAME
    payload = trajectory.to_json_dict()
    text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    try:
        out_path.write_text(text, encoding="utf-8")
    except OSError as exc:
        logger.warning("Could not write %s: %s", out_path, exc)
        return None
    logger.debug("Wrote %s", out_path)
    return out_path


def postprocess(root: Path | str) -> list[Path]:
    """Convert every run under ``root`` (tree or single run dir).

    Returns the list of written ``trajectory.json`` paths.
    """
    root = Path(root)
    written: list[Path] = []
    for run_dir in _iter_run_dirs(root):
        out = write_trajectory(run_dir)
        if out is not None:
            written.append(out)
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m sregym.traces.postprocess",
        description="Convert raw agent run logs into ATIF trajectory.json files.",
    )
    parser.add_argument(
        "path",
        type=Path,
        help="A results/ tree root or a single run_<n> directory.",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging.")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    if not args.path.exists():
        parser.error(f"path does not exist: {args.path}")

    written = postprocess(args.path)
    for out in written:
        print(out)
    logger.info("Wrote %d trajectory file(s).", len(written))
    return 0


if __name__ == "__main__":
    sys.exit(main())
