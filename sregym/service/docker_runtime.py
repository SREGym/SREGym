"""Select an explicit Docker endpoint without changing process globals."""

import os


def docker_command(*arguments: str, host: str | None = None) -> list[str]:
    return ["docker", *(["--host", host] if host else []), *arguments]


def trusted_docker_host() -> str | None:
    host = os.environ.get("SREGYM_TRUSTED_DOCKER_HOST") or None
    if rootless_workload_enabled() and not host:
        raise ValueError("Rootless runs require an explicit trusted Docker endpoint")
    return host


def rootless_workload_enabled() -> bool:
    return os.environ.get("SREGYM_ROOTLESS_WORKLOAD") == "1"
