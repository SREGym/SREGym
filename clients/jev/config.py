"""Jev experiment configuration, without SDK or MCP imports."""

import json
import os
import sys
from pathlib import Path

MODEL_ENV = "AGENT_JEV_MODEL"
KEY_ENV = "TYPESAFE_API_KEY"

INSTRUCTION = """
DECISION SUPPORT
Use `jev_plan` after the first observations, before choosing a root cause.
Propose 3-5 competing explanations with a read-only test for each.
State what result would support or reject each explanation.
Do not propose only variants of the same explanation.
Include raw symptoms, fresh request results, and contrary evidence.
The tool adds a current namespace snapshot and ranks the proposed tests.
It does not execute them or reveal a known answer.

Read the returned test priorities, check safety, and run the selected tests separately.
When the ranking is uncertain, run both suggested checks.
Use the observed outcomes to reject explanations, not just accumulate supporting logs.
If the tests do not identify a cause, propose different tests through `jev_plan`.
Historical startup errors and Ready Pods alone do not establish the current problem.

Use `jev_submit` for diagnosis before applying a repair. Supply the causal mechanism
and fresh evidence that distinguishes it from alternatives. Wait for accepted.
If the review refuses submission, the tool requires a new `jev_plan` before retrying.
Propose tests that can disprove the rejected explanation and examine other application operations.
Run the selected checks and supply their results. Rewording the same claim is not new evidence.
After diagnosis is accepted, apply the smallest mechanism-level repair.
Test the original failing behavior and unaffected operations, then use `jev_submit` for mitigation.
Check that the fix remains valid during ordinary restarts, placement changes, and requests.

The planner uses Choice and Score; submission uses separate Noul evidence questions.
Confidence and probability are experimental guidance, not guarantees of correctness.
Never treat a high test ranking as proof of its cause.
If Jev fails, report the error and continue investigating; do not assume submission succeeded.
Do not send passwords, tokens, or API keys.
"""

SUBMISSION_INSTRUCTION = """HOW TO SUBMIT:

Use `jev_submit` for both stages instead of a direct HTTP submission.
For diagnosis, set stage=diagnosis and supply your causal explanation and fresh evidence.
For mitigation, set stage=mitigation and supply the diagnosis, applied_action, and before/after evidence.
The tool submits only when all required evidence scores reach the existing supported band (at least 0.7).
Only status=accepted confirms submission. not_submitted means no submission occurred.
When review refuses submission, call `jev_plan`, run new diagnostic tests, and then retry.
Do not bypass the review with curl or report completion without accepted submissions.
If diagnosis is still being graded, wait and retry mitigation after its stage changes.
These scores are experimental evidence checks, not guarantees of correctness.
"""


def configure_experiment(args) -> None:
    """Reject unsupported runs before deployment and clear stale opt-in state."""
    model = getattr(args, "jev_model", None)
    if model is None:
        os.environ.pop(MODEL_ENV, None)
        return
    if not model.strip():
        raise ValueError("--jev-model must not be empty")
    if args.agent != "codex" or args.use_external_harness:
        raise ValueError("--jev-model requires the built-in Codex agent")
    if not os.environ.get(KEY_ENV, "").strip():
        raise ValueError("--jev-model requires TYPESAFE_API_KEY")
    if not args.force_build:
        raise ValueError("--jev-model currently requires --force-build to include the experimental tool")
    os.environ[MODEL_ENV] = model.strip()


def codex_args(logs_dir: Path) -> list[str]:
    """Configure only this Codex process; never edit the user's config.toml."""
    if not os.environ.get(MODEL_ENV):
        return []
    if not os.environ.get(KEY_ENV, "").strip():
        raise ValueError("Jev requires TYPESAFE_API_KEY")
    # Codex filters child environments. Explicitly retain proxy and CA variables
    # so the stdio server uses the same network policy as the agent's commands.
    forwarded = [
        KEY_ENV,
        MODEL_ENV,
        "PYTHONPATH",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "no_proxy",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "API_HOSTNAME",
        "API_PORT",
    ]
    values = {
        "command": sys.executable,
        "args": ["-m", "clients.jev.server", "--log-path", str(logs_dir.resolve() / "jev_calls.jsonl")],
        "env_vars": forwarded,
        "required": True,
        "startup_timeout_sec": 15,
        "tool_timeout_sec": 75,
        "enabled_tools": ["jev_plan", "jev_submit"],
    }
    return [arg for name, value in values.items() for arg in ("-c", f"mcp_servers.jev.{name}={json.dumps(value)}")]
