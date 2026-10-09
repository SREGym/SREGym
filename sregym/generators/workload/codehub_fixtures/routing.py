"""Routing helpers used by the request-router service."""


def choose_region(regions):
    """Choose a stable destination from the healthy region inventory."""
    return sorted(regions)[0]


def retry_delays(attempts, initial_seconds=0.1, maximum_seconds=2.0):
    if attempts < 0:
        raise ValueError("Attempts cannot be negative")
    return [min(initial_seconds * (2**attempt), maximum_seconds) for attempt in range(attempts)]
