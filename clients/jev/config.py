"""Jev experiment configuration, without SDK or MCP imports."""

import json
import os
import sys
from pathlib import Path

MODEL_ENV = "AGENT_JEV_MODEL"
KEY_ENV = "TYPESAFE_API_KEY"

INSTRUCTION = """
DECISION SUPPORT
Use `jev_evaluate` before choosing a diagnostic direction, submitting a diagnosis,
applying a mitigation, or declaring recovery. Supply the relevant observations and
candidate decisions. Ask narrow questions and include an insufficient-evidence
option where applicable. Treat Jev results as advice, not proof. Verify the effects
of each change with fresh observations. If Jev fails, report the tool error and
continue with your own reasoning. Do not repeat an unchanged request without new
evidence or a different question. Do not send passwords, tokens, or API keys.
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
    ]
    values = {
        "command": sys.executable,
        "args": ["-m", "clients.jev.server", "--log-path", str(logs_dir.resolve() / "jev_calls.jsonl")],
        "env_vars": forwarded,
        "required": True,
        "startup_timeout_sec": 15,
        "tool_timeout_sec": 45,
        "enabled_tools": ["jev_evaluate"],
    }
    return [arg for name, value in values.items() for arg in ("-c", f"mcp_servers.jev.{name}={json.dumps(value)}")]
