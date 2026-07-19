"""Build the operational-context system message AURA receives per problem.

Closes Constitution Principle II's enforcement boundary for SREGym runs:

- The ``user`` message body is ALWAYS the Conductor's verbatim instruction
  (built by SREGym, passed through to AURA byte-for-byte).
- The ``system`` message body is the ONLY place operational context (problem
  id, namespace, submission target, available MCP servers) is allowed to
  appear.

The builder is a pure function — no I/O, no env-var lookups — so the tests
in ``tests/test_sregym_system_message_builder.py`` can lock down the exact
string the LLM sees per problem.

Deliberately signature-restricted: there is NO ``instruction`` parameter
on this function. T020a includes a defense test asserting the builder
refuses an ``instruction`` kwarg — that prevents a future contributor
from "helpfully" inlining the problem prompt into the system message and
silently breaking Principle II.

Refactored 2026-05-19 (rationale summarized in ``docs/sregym.md``):
SREGym's MCP server is one fastmcp/SSE host. The active AURA bridge set is
kubectl, jaeger, and prometheus; Loki is omitted, and submit is handled by
the driver posting the final answer to the Conductor HTTP endpoint. The
system message lists the names of the bridged AURA MCP servers (the actual
URLs live in AURA's TOML and are resolved at AURA spawn — see
config/aura-sregym.toml.template).
"""

from __future__ import annotations

from typing import Iterable

DEFAULT_MCP_SERVERS: tuple[str, ...] = (
    "sregym_kubectl",
    "sregym_jaeger",
    "sregym_prometheus",
)

SYSTEM_MESSAGE_TEMPLATE = """\
You are an experienced SRE working a SREGym benchmark problem.

Problem id: {problem_id}
Application namespace: {namespace}

Available MCP servers (bridged from SREGym's fastmcp/SSE host via fastmcp-proxy):
{server_block}

Write your final root-cause analysis as the LAST assistant message in this
conversation. The harness submits that message verbatim to the SREGym grader
via HTTP — there is NO `submit` MCP tool to call; just produce the answer
text and the orchestrator handles posting it.{window_block}
"""

WINDOW_BLOCK_TEMPLATE = "\n\nIncident window hint: {window_hint}."


def build_sregym_system_message(
    *,
    problem_id: str,
    namespace: str,
    submission_endpoint: str,
    mcp_servers: Iterable[str] = DEFAULT_MCP_SERVERS,
    window_hint: str | None = None,
) -> str:
    """Return the system message AURA sees for a single SREGym problem.

    Parameters
    ----------
    problem_id
        Verbatim SREGym problem identifier.
    namespace
        Kubernetes namespace of the target application.
    submission_endpoint
        The Conductor's ``{api_url}/submit`` URL, included for traceability.
        The driver submits the final assistant message to this endpoint;
        there is no submit MCP tool on this path.
    mcp_servers
        Names of the AURA-configured MCP servers (the per-mount keys from
        ``config/aura-sregym.toml.template``). Defaults to the canonical
        kubectl/jaeger/prometheus bridge set; tests inject smaller subsets
        to confirm rendering.
    window_hint
        Optional short string the orchestrator may include when the
        Conductor provides a known time window for the incident.

    Notes
    -----
    The function intentionally takes no ``instruction`` parameter. The user
    message is the verbatim SREGym instruction; embedding it here would
    violate Principle II.
    """
    server_lines = [f"  - {name}" for name in mcp_servers]
    server_block = "\n".join(server_lines) if server_lines else "  (none)"
    window_block = (
        WINDOW_BLOCK_TEMPLATE.format(window_hint=window_hint)
        if window_hint
        else ""
    )
    return SYSTEM_MESSAGE_TEMPLATE.format(
        problem_id=problem_id,
        namespace=namespace,
        submission_endpoint=submission_endpoint,
        server_block=server_block,
        window_block=window_block,
    )
