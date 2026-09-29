"""Deployment profile selection.

SREGym deploys a fixed set of infrastructure and application components. Some of
them are never read by the harness, the oracles, the MCP tools or the agent, but
still cost real memory and CPU on the cluster. On a workstation-sized cluster
that overhead is the difference between a suite that runs and one that OOMKills.

The ``svelte`` profile removes those components. It is opt-in because it changes
what an agent can observe in the cluster, so results produced under ``svelte`` are
not directly comparable with results produced under ``full``.

Read the profile with :func:`is_svelte`. It is a module-level singleton rather than
a field on ``ConductorConfig`` because ``Application`` instances are constructed
deep inside ``ProblemRegistry`` with no access to the conductor's config.
"""

import logging
import os

FULL = "full"
SVELTE = "svelte"
PROFILES = (FULL, SVELTE)

_ENV_VAR = "SREGYM_PROFILE"

logger = logging.getLogger("all.sregym.profile")

_profile: str | None = None


def set_profile(profile: str) -> None:
    """Set the active profile. Called once from main.py before any deployment."""
    global _profile
    if profile not in PROFILES:
        raise ValueError(f"Unknown profile {profile!r}; expected one of {PROFILES}")
    _profile = profile
    logger.info(f"Deployment profile: {profile}")


def get_profile() -> str:
    """Return the active profile, falling back to $SREGYM_PROFILE then 'full'.

    The env fallback exists so that code paths which bypass main.py (tests, the
    measurement harness, external harnesses) can still select a profile.
    """
    if _profile is not None:
        return _profile
    env = os.environ.get(_ENV_VAR)
    if env:
        if env not in PROFILES:
            raise ValueError(f"{_ENV_VAR}={env!r} is not one of {PROFILES}")
        return env
    return FULL


def is_svelte() -> bool:
    return get_profile() == SVELTE
