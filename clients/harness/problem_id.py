"""Resolve benchmark problem_id for harness log/artifact naming (not exposed via conductor API)."""

import logging
import os

logger = logging.getLogger(__name__)

# Set by main.py before launching an agent; forwarded into containers by AgentLauncher.
HARNESS_PROBLEM_ID_ENV = "SREGYM_PROBLEM_ID"


def resolve_problem_id(*, cli_problem_id: str | None = None) -> str:
    """
    Resolve problem_id for driver artifacts.

    Resolution order:
    1. cli_problem_id (--problem-id for standalone driver runs)
    2. SREGYM_PROBLEM_ID env (set by main.py / AgentLauncher for benchmark runs)
    3. "unknown" with a warning

    No problem-id file is written under AGENT_LOGS_DIR: eval agents share /logs and could read it.
    """
    if cli_problem_id:
        return cli_problem_id

    env_id = os.environ.get(HARNESS_PROBLEM_ID_ENV)
    if env_id:
        return env_id

    logger.warning(
        "Could not resolve problem_id (set %s, pass --problem-id, or run via main.py); using 'unknown'",
        HARNESS_PROBLEM_ID_ENV,
    )
    return "unknown"
