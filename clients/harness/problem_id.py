"""Resolve harness artifact identity for driver log/artifact naming."""

import logging
import os

logger = logging.getLogger(__name__)

# Set by main.py before launching a benchmark agent; forwarded into containers by AgentLauncher.
HARNESS_ARTIFACT_ID_ENV = "SREGYM_ARTIFACT_ID"
HARNESS_PROBLEM_ID_ENV = "SREGYM_PROBLEM_ID"


def resolve_problem_id(*, cli_problem_id: str | None = None) -> str:
    """
    Resolve artifact identity for driver artifacts.

    Resolution order:
    1. cli_problem_id (--problem-id for standalone driver runs)
    2. SREGYM_ARTIFACT_ID env (set by main.py / AgentLauncher for benchmark runs)
    3. SREGYM_PROBLEM_ID for legacy standalone driver use
    4. "unknown" with a warning

    No problem-id file is written under AGENT_LOGS_DIR: eval agents share /logs and could read it.
    """
    if cli_problem_id:
        return cli_problem_id

    artifact_id = os.environ.get(HARNESS_ARTIFACT_ID_ENV)
    if artifact_id:
        return artifact_id

    problem_id = os.environ.get(HARNESS_PROBLEM_ID_ENV)
    if problem_id:
        return problem_id

    logger.warning(
        "Could not resolve artifact identity (set %s, pass --problem-id, or run via main.py); using 'unknown'",
        HARNESS_ARTIFACT_ID_ENV,
    )
    return "unknown"
