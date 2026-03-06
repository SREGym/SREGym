import logging
import os

from dotenv import load_dotenv

from llm_backend.get_llm_backend import LiteLLMBackend

load_dotenv()

logger = logging.getLogger(__name__)

_PROVIDER_API_KEY_ENV: dict[str, str | None] = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "azure": None,  # uses AZURE_API_KEY / AZURE_API_BASE / AZURE_API_VERSION
    "huggingface": "HUGGINGFACE_API_KEY",
    "bedrock": None,  # uses AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY
    "vertex_ai": None,  # uses VERTEXAI_PROJECT / VERTEXAI_LOCATION + gcloud ADC
    "watsonx": None,  # uses WATSONX_API_KEY / WATSONX_URL / WX_PROJECT_ID
}

_PREFIX_TO_BACKEND_PROVIDER: dict[str, str] = {
    "openai": "openai",
    "watsonx": "watsonx",
}

_WATSONX_DEFAULT_URL = "https://us-south.ml.cloud.ibm.com"


def get_llm_backend_for_tools(
    model: str | None = None,
    temperature: float = 0.0,
    top_p: float = 0.95,
    max_tokens: int | None = None,
) -> LiteLLMBackend:
    model = model or os.environ.get("MODEL_ID", "openai/gpt-4o")
    logger.info(f"Using model: {model}")

    parts = model.split("/", 1)
    if len(parts) < 2:
        raise ValueError(f"Model must be in 'provider/model-name' format, got: {model!r}")
    provider_prefix = parts[0].lower()

    backend_provider = _PREFIX_TO_BACKEND_PROVIDER.get(provider_prefix, "litellm")

    key_env = _PROVIDER_API_KEY_ENV.get(provider_prefix)
    api_key = os.environ.get(key_env) if key_env else None

    extra_kwargs: dict = {}
    if provider_prefix == "watsonx":
        wx_project_id = os.environ.get("WX_PROJECT_ID")
        if not wx_project_id:
            raise ValueError("WX_PROJECT_ID environment variable is required for WatsonX models")
        api_key = os.environ.get("WATSONX_API_KEY")
        if not api_key:
            raise ValueError("WATSONX_API_KEY environment variable is required for WatsonX models")
        extra_kwargs["wx_project_id"] = wx_project_id
        extra_kwargs["url"] = os.environ.get("WATSONX_URL", _WATSONX_DEFAULT_URL)

    return LiteLLMBackend(
        provider=backend_provider,
        model_name=model,
        api_key=api_key,
        temperature=temperature,
        top_p=top_p,
        max_tokens=max_tokens,
        **extra_kwargs,
    )
