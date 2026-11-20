"""Prompt text and a check for a problem whose environment is not the test cluster.

Such a problem's own description says what the agent can reach and with which
tools, so the opening and tool lines here name no cluster, namespace or cluster
tooling.
"""

OPENING = """You are an SRE agent tasked with diagnosing and fixing issues in an application.

Application: {app_name}"""

TOOLS = "- Use the access and tools described above to inspect and change the application"


def outside_cluster_opening(app_name: str) -> tuple[str, str]:
    """Return the opening (through the application line) and the tool lines."""
    return OPENING.format(app_name=app_name), TOOLS


def require_cluster(app_info: dict, client: str) -> None:
    """Stop a client built on cluster tools before it starts on a problem outside the cluster."""
    if not app_info.get("uses_cluster", True):
        raise SystemExit(f"{client} needs the test cluster, but this problem's environment is outside it.")
