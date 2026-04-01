import os

from llm_backend.get_llm_backend import LiteLLMBackend


def get_llm_backend(model_name: str) -> LiteLLMBackend:
    """Initialize an LLM backend for the given litellm model string."""
    print(f"🔧 Initializing LLM backend — model: {model_name}")
    return LiteLLMBackend(model_name=model_name)


def get_llm_backend_for_agent() -> LiteLLMBackend:
    """Get LLM backend for agent tasks"""
    model_id = os.environ.get("AGENT_MODEL_ID")
    if not model_id:
        raise ValueError("AGENT_MODEL_ID environment variable is not set.")
    return get_llm_backend(model_id)


def get_llm_backend_for_judge() -> LiteLLMBackend:
    """Get LLM backend for the LLM-as-a-judge evaluator."""
    model_id = os.environ.get("JUDGE_MODEL_ID")
    if not model_id:
        raise ValueError("JUDGE_MODEL_ID environment variable is not set.")
    return get_llm_backend(model_id)
