import os

from llm_backend.get_llm_backend import LiteLLMBackend

# With adaptive thinking on, thinking tokens count against max_tokens on Anthropic
# models. The judge's historical default (4096) can truncate the checklist JSON.
JUDGE_MIN_MAX_TOKENS_WITH_REASONING = 16000


def get_llm_backend(
    model_name: str,
    api_base: str | None = None,
    api_key: str | None = None,
    provider: str | None = None,
    temperature: float = 0.0,
    max_tokens: int | None = None,
    usage_available: bool = True,
    retry: bool = True,
    reasoning_effort: str | None = None,
) -> LiteLLMBackend:
    """Initialize an LLM backend for the given litellm model string."""
    endpoint_status = "set" if api_base else "unset"
    effort_status = f", reasoning_effort: {reasoning_effort}" if reasoning_effort else ""
    print(f"🔧 Initializing LLM backend — model: {model_name}, api_base: {endpoint_status}{effort_status}")
    return LiteLLMBackend(
        model_name=model_name,
        api_base=api_base,
        api_key=api_key,
        provider=provider,
        temperature=temperature,
        max_tokens=max_tokens,
        usage_available=usage_available,
        retry=retry,
        reasoning_effort=reasoning_effort,
    )


def get_llm_backend_for_agent() -> LiteLLMBackend:
    """Get LLM backend for agent tasks"""
    model_id = os.environ.get("AGENT_MODEL_ID")
    if not model_id:
        raise ValueError("AGENT_MODEL_ID environment variable is not set.")
    return get_llm_backend(
        model_id,
        api_base=os.environ.get("AGENT_API_BASE"),
        api_key=os.environ.get("AGENT_API_KEY"),
    )


def get_llm_backend_for_judge(
    *,
    provider: str | None = None,
    model_name: str | None = None,
    api_base: str | None = None,
    api_key: str | None = None,
    temperature: float = 0.0,
    max_tokens: int | None = None,
    reasoning_effort: str | None = None,
) -> LiteLLMBackend:
    """Get LLM backend for the LLM-as-a-judge evaluator.

    ``reasoning_effort`` defaults to ``JUDGE_REASONING_EFFORT`` (set by ``--judge-reasoning-effort``).
    ``JUDGE_MAX_TOKENS`` overrides the caller's ``max_tokens``; otherwise, when an effort is set,
    ``max_tokens`` is raised to at least ``JUDGE_MIN_MAX_TOKENS_WITH_REASONING`` so thinking cannot
    starve the answer.
    """
    model_id = model_name or os.environ.get("JUDGE_MODEL_ID")
    if not model_id:
        raise ValueError("A judge model must be passed or set in JUDGE_MODEL_ID.")
    if bridge_url := os.environ.get("SREGYM_JUDGE_BRIDGE_URL"):
        # The selected CLI owns inference, even for Claude/native model names or
        # explicit oracle provider settings. Never fall through to API billing.
        provider, api_base, api_key = "openai", bridge_url, "dummy"
    effort = reasoning_effort or os.environ.get("JUDGE_REASONING_EFFORT") or None
    if effort == "none":
        effort = None
    if override := os.environ.get("JUDGE_MAX_TOKENS"):
        max_tokens = int(override)
    elif effort and (max_tokens or 0) < JUDGE_MIN_MAX_TOKENS_WITH_REASONING:
        max_tokens = JUDGE_MIN_MAX_TOKENS_WITH_REASONING
    return get_llm_backend(
        model_id,
        api_base=api_base if api_base is not None else os.environ.get("JUDGE_API_BASE"),
        api_key=api_key if api_key is not None else os.environ.get("JUDGE_API_KEY"),
        provider=provider,
        temperature=temperature,
        max_tokens=max_tokens,
        usage_available=not bool(bridge_url),
        retry=not bool(bridge_url),
        reasoning_effort=effort,
    )
