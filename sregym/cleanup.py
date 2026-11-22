"""Cleanup budget shared by the runner and application disposal."""

import os


def cleanup_drain_timeout() -> float:
    return float(os.environ.get("SREGYM_CLEANUP_DRAIN_TIMEOUT_S", "600"))
