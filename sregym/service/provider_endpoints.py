"""Resolve model and authentication endpoints before an agent starts."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from sregym.service.internet_policy import EndpointRule, InternetPolicy


@dataclass(frozen=True)
class ProviderEndpoint:
    default_url: str | None = None
    environment_variables: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()


# Keep a provider's default, configuration keys, and model aliases together.
# Providers without a default use the explicit authentication flows below.
PROVIDERS = {
    "anthropic": ProviderEndpoint("https://api.anthropic.com", ("ANTHROPIC_BASE_URL", "ANTHROPIC_API_BASE")),
    "deepseek": ProviderEndpoint("https://api.deepseek.com", ("DEEPSEEK_API_BASE",)),
    "gemini": ProviderEndpoint(
        "https://generativelanguage.googleapis.com", ("GEMINI_API_BASE",), ("google", "google_ai_studio")
    ),
    "groq": ProviderEndpoint("https://api.groq.com", ("GROQ_API_BASE",)),
    "huggingface": ProviderEndpoint("https://router.huggingface.co", ("HF_ENDPOINT",)),
    "mistral": ProviderEndpoint("https://api.mistral.ai", ("MISTRAL_API_BASE",)),
    "moonshot": ProviderEndpoint("https://api.moonshot.ai", ("MOONSHOT_API_BASE",)),
    "openai": ProviderEndpoint("https://api.openai.com", ("OPENAI_API_BASE", "OPENAI_BASE_URL")),
    "openrouter": ProviderEndpoint("https://openrouter.ai", ("OPENROUTER_API_BASE",)),
    "xai": ProviderEndpoint("https://api.x.ai", ("XAI_API_BASE",)),
    "zai-coding-plan": ProviderEndpoint("https://api.z.ai", ("ZAI_API_BASE",), ("zai", "zhipu")),
    "bedrock": ProviderEndpoint(aliases=("amazon-bedrock",)),
    "vertex": ProviderEndpoint(aliases=("vertex_ai",)),
    "azure": ProviderEndpoint(),
    "watsonx": ProviderEndpoint(),
    "custom": ProviderEndpoint(aliases=("custom_openai", "ollama", "ollama_chat")),
    "github-copilot": ProviderEndpoint("https://api.githubcopilot.com"),
    "llama": ProviderEndpoint("https://api.llama.com"),
    "opencode": ProviderEndpoint("https://opencode.ai"),
}


def provider_endpoint_rules(
    policy: InternetPolicy,
    environment: Mapping[str, str],
    *,
    codex_auth_available: bool = False,
) -> tuple[EndpointRule, ...]:
    """Return the fixed provider destinations for one evaluated agent."""
    if not policy.is_filtered or not policy.agent_name:
        return ()

    agent = policy.agent_name.casefold()
    model = (policy.model_id or "").strip()

    if agent in {"autosubmit", "debug"}:
        return ()
    if agent == "codex":
        custom_base = _configured_url(environment, "AGENT_API_BASE")
        if custom_base:
            return (EndpointRule.host_from_url(custom_base),)
        if codex_auth_available:
            return _rules_from_urls(("https://chatgpt.com", "https://auth.openai.com"))
        return _rules_from_urls((_provider_url("openai", environment),))
    if agent == "claudecode":
        urls = [_provider_url("anthropic", environment)]
        if environment.get("CLAUDE_CODE_OAUTH_TOKEN", "").strip():
            urls.append("https://platform.claude.com")
        return _rules_from_urls(urls)
    if agent == "gemini":
        provider = "vertex" if _uses_vertex_ai(environment) else "gemini"
        return _provider_rules(provider, environment)
    if agent == "copilot":
        custom_base = _configured_url(environment, "COPILOT_PROVIDER_BASE_URL")
        if custom_base:
            return (EndpointRule.host_from_url(custom_base),)
        return _provider_rules("github-copilot", environment)
    if agent == "opencode":
        provider = model.partition("/")[0].casefold()
        if not provider or "/" not in model:
            raise ValueError("Filtered internet access requires an OpenCode model in provider/model format")
        if provider == "local":
            return _required_custom_base(environment, agent, model)
        return _provider_rules(provider, environment)
    if agent == "stratus":
        if _configured_url(environment, "AGENT_API_BASE"):
            return _required_custom_base(environment, agent, model)
        return _provider_rules(_provider_from_model(model), environment)

    raise ValueError(
        f"Filtered internet access does not know the model provider for agent '{policy.agent_name}'. "
        "Use a supported agent or run with --internet-access open."
    )


def _provider_from_model(model: str) -> str:
    normalized = model.strip().casefold()
    prefix = normalized.partition("/")[0]
    if "/" in normalized:
        for name, endpoint in PROVIDERS.items():
            if prefix == name or prefix in endpoint.aliases:
                return name
    if normalized.startswith("claude"):
        return "anthropic"
    if normalized.startswith("gemini"):
        return "gemini"
    if normalized.startswith("deepseek"):
        return "deepseek"
    if normalized.startswith(("gpt-", "o1", "o3", "o4", "ft:")):
        return "openai"
    raise ValueError(
        f"Filtered internet access cannot determine the provider for model '{model}'. "
        "Set AGENT_API_BASE to the model API endpoint or use --internet-access open."
    )


def _provider_rules(provider: str, environment: Mapping[str, str]) -> tuple[EndpointRule, ...]:
    provider = provider.casefold()
    if provider == "google":
        provider = "vertex" if _uses_vertex_ai(environment) else "gemini"
    if provider == "custom":
        return _required_custom_base(environment, "stratus", environment.get("AGENT_MODEL_ID", ""))
    if provider in {"amazon-bedrock", "bedrock"}:
        region = _first_value(environment, "AWS_REGION_NAME", "AWS_REGION", "AWS_DEFAULT_REGION")
        if not region:
            raise ValueError("Filtered Bedrock access requires AWS_REGION_NAME, AWS_REGION, or AWS_DEFAULT_REGION")
        runtime = _configured_url(environment, "AWS_BEDROCK_RUNTIME_ENDPOINT")
        sts = _configured_url(environment, "AWS_STS_ENDPOINT")
        return _rules_from_urls(
            (
                runtime or f"https://bedrock-runtime.{region}.amazonaws.com",
                sts or f"https://sts.{region}.amazonaws.com",
            )
        )
    if provider == "vertex":
        location = _first_value(environment, "VERTEXAI_LOCATION", "VERTEX_LOCATION", "GOOGLE_CLOUD_LOCATION")
        if not location:
            location = "us-central1"
        return _rules_from_urls(
            (
                f"https://{location}-aiplatform.googleapis.com",
                "https://oauth2.googleapis.com",
            )
        )
    if provider == "azure":
        endpoint = _configured_url(environment, "AZURE_API_BASE", "AZURE_OPENAI_ENDPOINT")
        if endpoint:
            return (EndpointRule.host_from_url(endpoint),)
        resource = environment.get("AZURE_RESOURCE_NAME", "").strip()
        if resource:
            return _rules_from_urls((f"https://{resource}.openai.azure.com",))
        raise ValueError("Filtered Azure access requires AZURE_API_BASE, AZURE_OPENAI_ENDPOINT, or AZURE_RESOURCE_NAME")
    if provider == "watsonx":
        endpoint = _configured_url(environment, "WATSONX_API_BASE", "WATSONX_URL", "WX_URL", "WML_URL")
        if not endpoint:
            raise ValueError("Filtered WatsonX access requires WATSONX_API_BASE, WATSONX_URL, WX_URL, or WML_URL")
        iam = _configured_url(environment, "WATSONX_IAM_URL") or "https://iam.cloud.ibm.com"
        return _rules_from_urls((endpoint, iam))
    return _rules_from_urls((_provider_url(provider, environment),))


def _provider_url(provider: str, environment: Mapping[str, str]) -> str:
    endpoint = PROVIDERS.get(provider)
    if endpoint is None or endpoint.default_url is None:
        raise ValueError(
            f"Filtered internet access does not know the endpoint for provider '{provider}'. "
            "Set AGENT_API_BASE with a supported custom model or use --internet-access open."
        )
    return _configured_url(environment, *endpoint.environment_variables) or endpoint.default_url


def _required_custom_base(
    environment: Mapping[str, str],
    agent: str,
    model: str,
) -> tuple[EndpointRule, ...]:
    endpoint = _configured_url(environment, "AGENT_API_BASE")
    if not endpoint:
        raise ValueError(f"Filtered internet access requires AGENT_API_BASE for agent '{agent}' and model '{model}'")
    return (EndpointRule.host_from_url(endpoint),)


def _configured_url(environment: Mapping[str, str], *names: str) -> str | None:
    for name in names:
        value = environment.get(name, "").strip()
        if not value:
            continue
        try:
            EndpointRule.host_from_url(value)
        except ValueError as exc:
            raise ValueError(f"{name} must be an HTTP(S) URL") from exc
        return value
    return None


def _first_value(environment: Mapping[str, str], *names: str) -> str | None:
    return next((environment[name].strip() for name in names if environment.get(name, "").strip()), None)


def _rules_from_urls(urls: tuple[str, ...] | list[str]) -> tuple[EndpointRule, ...]:
    return tuple(sorted({EndpointRule.host_from_url(url) for url in urls}))


def _uses_vertex_ai(environment: Mapping[str, str]) -> bool:
    return environment.get("GOOGLE_GENAI_USE_VERTEXAI", "").strip().casefold() in {"1", "true", "yes"}
