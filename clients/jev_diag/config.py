"""Run configuration for the jev_diag agent. No SDK imports: main.py imports this at startup."""

from __future__ import annotations

import os

from clients.jev.config import KEY_ENV, MODEL_ENV

AGENT_NAME = "jev_diag"
AGENT_NAMES = {"jev_diag", "jev_agent"}  # jev_agent (clients/jev_agent) uses the same decision backends
DEFAULT_MODEL = "jev-latest"


def configure(args) -> None:
    """Take the Jev model from ``--model`` for ``--agent jev_diag`` and reject unsupported runs early.

    The model goes into AGENT_JEV_MODEL, the variable ``--jev-model`` sets for Codex decision support.
    The container runner forwards TYPESAFE_API_KEY only while it is set, so other runs never receive the
    key. Because ``--model`` names a Jev model here, the judge needs its own ``--judge-model``.
    """
    agent = getattr(args, "agent", None)
    if agent not in AGENT_NAMES or getattr(args, "use_external_harness", False):
        return
    model = (getattr(args, "model", None) or DEFAULT_MODEL).strip()
    if not model:
        raise ValueError(f"--model must name a Jev model for --agent {agent}")
    if not getattr(args, "judge_model", None):
        raise ValueError(f"--agent {agent} takes the Jev model from --model; set the judge's model with --judge-model")
    if os.environ.get("JEV_DIAG_BACKEND", "").strip() == "openai_decisions":
        if not os.environ.get("DECISIONS_OPENAI_API_KEY", "").strip():
            raise ValueError("JEV_DIAG_BACKEND=openai_decisions requires DECISIONS_OPENAI_API_KEY")
    elif os.environ.get("JEV_DIAG_BACKEND", "").strip() == "openrouter":
        if not os.environ.get("DECISIONS_OPENROUTER_API_KEY", "").strip():
            raise ValueError("JEV_DIAG_BACKEND=openrouter requires DECISIONS_OPENROUTER_API_KEY")
    elif not os.environ.get(KEY_ENV, "").strip():
        raise ValueError(f"--agent {agent} requires TYPESAFE_API_KEY")
    args.model = model
    os.environ[MODEL_ENV] = model


def jev_model() -> str | None:
    """The Jev model for this run.

    main.py sets AGENT_JEV_MODEL from ``--model`` for ``--agent jev_diag``. A standalone run falls back
    to TYPESAFE_DEFAULT_MODEL and then to the SDK default (jev-latest).
    """
    return os.getenv(MODEL_ENV) or os.getenv("TYPESAFE_DEFAULT_MODEL") or None
