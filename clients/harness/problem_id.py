"""Resolve benchmark problem_id for harness log/artifact naming (not exposed to eval agents)."""

import logging
import os
import re
from pathlib import Path

logger = logging.getLogger(__name__)

_RUN_DIR = re.compile(r"^run_\d+$", re.IGNORECASE)


def resolve_problem_id(
    *,
    cli_problem_id: str | None = None,
    logs_dir: Path | str | None = None,
) -> str:
    """
    Resolve problem_id for driver artifacts without calling the conductor API.

    Resolution order:
    1. cli_problem_id (--problem-id for standalone runs)
    2. logs_dir or AGENT_LOGS_DIR (parent of run_<n> when using main.py layout)
    3. "unknown" with a warning
    """
    if cli_problem_id:
        return cli_problem_id

    raw = logs_dir or os.environ.get("AGENT_LOGS_DIR")
    if not raw:
        logger.warning("Could not resolve problem_id; using 'unknown'")
        return "unknown"

    path = Path(raw).resolve()
    if _RUN_DIR.match(path.name) and path.parent.name:
        return path.parent.name
    if path.name and path.name != "unknown":
        return path.name

    logger.warning("Could not resolve problem_id from %s; using 'unknown'", path)
    return "unknown"
