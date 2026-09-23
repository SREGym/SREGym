"""Load API keys and other settings from a local ``.env`` file.

The file is optional and git-ignored. Values already present in the process
environment win, so a shell ``export`` still overrides the file. Point
``SREGYM_ENV_FILE`` at another path to load a different file.

Loaded here means visible to everything downstream: the judge backend on the
host and, through ``ContainerRunner``'s pass-through list, the agent container.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from dotenv import dotenv_values

from sregym.paths import BASE_PARENT_DIR

logger = logging.getLogger("all.sregym.env_file")

ENV_FILE_VAR = "SREGYM_ENV_FILE"
DEFAULT_ENV_FILE = BASE_PARENT_DIR / ".env"


def load_env_file(path: str | Path | None = None, *, override: bool = False) -> Path | None:
    """Merge ``.env`` into ``os.environ``. Returns the file used, or None when there is none.

    Only variable names are logged; values never are.
    """
    candidate = Path(path or os.environ.get(ENV_FILE_VAR) or DEFAULT_ENV_FILE).expanduser()
    if not candidate.is_file():
        return None
    values = {k: v for k, v in dotenv_values(candidate).items() if v is not None}
    applied, skipped = [], []
    for key, value in values.items():
        if not override and os.environ.get(key):
            skipped.append(key)
            continue
        os.environ[key] = value
        applied.append(key)
    if applied or skipped:
        logger.info(
            "Loaded %s: set %s%s",
            candidate,
            ", ".join(applied) or "nothing",
            f" (kept shell values for {', '.join(skipped)})" if skipped else "",
        )
    return candidate
