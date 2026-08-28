# Integration checks

## Codex-to-Z.ai bridge

Run the Codex bridge check before changing the custom provider, LiteLLM, or the
problem-difficulty workflow:

```bash
uv run python tests/integration/codex_litellm_bridge.py
```

The check launches the pinned LiteLLM proxy, invokes both the repository's real
Codex preflight and agent command, and sends their requests to a local mock of
the Z.ai Chat Completions endpoint. The mock rejects non-function tools, verifies
the upstream route, and requires the shell tools needed by the agent. It does not
read `ZCODE_API_KEY` or consume model credits.

Prerequisites: `codex` and `uvx` must be available in `PATH`. To test another
LiteLLM release, pass `--litellm-version VERSION`.
