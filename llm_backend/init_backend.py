"""Adopted from previous project"""

import os

from dotenv import load_dotenv

from llm_backend.get_llm_backend import LiteLLMBackend

load_dotenv()


def get_env_or_none(key: str, default=None):
    """Get environment variable value, returning None if not set or empty."""
    value = os.environ.get(key, default)
    if value == "" or value is None:
        return None
    return value


def get_llm_backend_for_tools():
    """
    Create LLM backend using environment variables set from CLI arguments.

    Environment variables:
    - MODEL_NAME: Model name in LiteLLM format (e.g., 'openai/gpt-4o', 'anthropic/claude-sonnet-4')
    - MODEL_PROVIDER: Provider type (openai, litellm, watsonx)
    - MODEL_API_KEY: API key (optional if set in provider-specific env vars)
    - MODEL_URL: Base URL for API (optional)
    - MODEL_TEMPERATURE: Temperature (default: 0.0)
    - MODEL_TOP_P: Top-p sampling (default: 0.95)
    - MODEL_MAX_TOKENS: Max tokens (optional)
    - MODEL_SEED: Random seed (optional)
    - MODEL_WX_PROJECT_ID: WatsonX project ID (required for watsonx)

    Falls back to legacy MODEL_ID for backward compatibility.
    """

    # Check for new-style configuration
    model_name = get_env_or_none("MODEL_NAME")
    provider = get_env_or_none("MODEL_PROVIDER", "openai")

    # Legacy fallback: if MODEL_NAME not set, check for MODEL_ID
    if not model_name:
        model_id = get_env_or_none("MODEL_ID", "gpt-4o")
        print(f"[DEPRECATED] Using MODEL_ID='{model_id}' - please migrate to --model CLI argument")
        print("Attempting to infer model name and provider from legacy MODEL_ID...")

        # Try to infer provider and model name from common patterns
        if model_id.startswith("gpt-") or model_id in ["gpt-4o", "gpt-5", "gpt-5-nano", "gpt-5-mini"]:
            provider = "openai"
            model_name = model_id
        elif "claude" in model_id:
            provider = "litellm"
            model_name = f"anthropic/{model_id}"
        elif "gemini" in model_id:
            provider = "litellm"
            model_name = f"gemini/{model_id}"
        elif model_id == "watsonx-llama":
            provider = "watsonx"
            model_name = "meta-llama/llama-3-3-70b-instruct"
        elif model_id == "moonshot":
            provider = "litellm"
            model_name = "moonshot/moonshot"
        elif model_id == "glm-4":
            provider = "litellm"
            model_name = "openai/glm-4"
        elif "azure" in model_id:
            provider = "litellm"
            model_name = "azure/gpt-4o"
        elif "bedrock" in model_id:
            provider = "litellm"
            model_name = model_id.replace("bedrock-", "bedrock/")
        else:
            # Default fallback
            provider = "openai"
            model_name = model_id
            print(f"[WARNING] Could not infer provider for MODEL_ID='{model_id}', defaulting to openai")

    print(f"Initializing LLM backend with provider='{provider}', model_name='{model_name}'")

    # Get configuration parameters from environment
    api_key = get_env_or_none("MODEL_API_KEY")
    url = get_env_or_none("MODEL_URL")
    temperature = float(get_env_or_none("MODEL_TEMPERATURE", "0.0"))
    top_p = float(get_env_or_none("MODEL_TOP_P", "0.95"))
    max_tokens = get_env_or_none("MODEL_MAX_TOKENS")
    if max_tokens:
        max_tokens = int(max_tokens)
    seed = get_env_or_none("MODEL_SEED")
    if seed:
        seed = int(seed)
    wx_project_id = get_env_or_none("MODEL_WX_PROJECT_ID")

    # If no explicit API key, try to get from provider-specific env vars
    if not api_key:
        if "openai" in model_name.lower() or provider == "openai":
            api_key = get_env_or_none("OPENAI_API_KEY")
        elif "anthropic" in model_name.lower() or "claude" in model_name.lower():
            api_key = get_env_or_none("ANTHROPIC_API_KEY")
        elif "gemini" in model_name.lower():
            api_key = get_env_or_none("GEMINI_API_KEY")
        elif provider == "watsonx":
            api_key = get_env_or_none("WATSONX_API_KEY")
        elif "moonshot" in model_name.lower():
            api_key = get_env_or_none("MOONSHOT_API_KEY")
        elif "glm" in model_name.lower():
            api_key = get_env_or_none("GLM_API_KEY")
        elif "azure" in model_name.lower():
            api_key = get_env_or_none("AZURE_API_KEY")

    # Handle Azure-specific configuration
    if "azure" in model_name.lower():
        azure_version = get_env_or_none("AZURE_API_VERSION")
        if not azure_version:
            print("[WARNING] AZURE_API_VERSION not set, Azure calls may fail")
        if not url:
            url = get_env_or_none("AZURE_API_BASE")

    # Build configuration parameters
    config_params = {
        "provider": provider,
        "model_name": model_name,
        "api_key": api_key,
        "url": url,
        "temperature": temperature,
        "top_p": top_p,
        "max_tokens": max_tokens,
        "seed": seed,
    }

    # Add provider-specific parameters
    if provider == "watsonx":
        config_params["wx_project_id"] = wx_project_id
        if not wx_project_id:
            print("[ERROR] MODEL_WX_PROJECT_ID or WX_PROJECT_ID is required for watsonx provider")
            exit(1)

    # Remove None values for cleaner logging
    config_params = {k: v for k, v in config_params.items() if v is not None}

    print(f"Creating LiteLLMBackend with config: {config_params}")

    return LiteLLMBackend(**config_params)
