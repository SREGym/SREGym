"""Internet-access policy shared by the agent runner and egress proxy."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import unquote, urlsplit


class InternetAccessMode(StrEnum):
    OPEN = "open"
    FILTERED = "filtered"


@dataclass(frozen=True)
class InternetPolicy:
    mode: InternetAccessMode = InternetAccessMode.FILTERED
    agent_name: str | None = None
    model_id: str | None = None
    additional_allowed_endpoints: tuple[str, ...] = ()

    @classmethod
    def from_mode(
        cls,
        mode: str | InternetAccessMode,
        *,
        agent_name: str | None = None,
        model_id: str | None = None,
        additional_allowed_endpoints: tuple[str, ...] | list[str] = (),
    ) -> InternetPolicy:
        endpoints = tuple(additional_allowed_endpoints)
        for endpoint in endpoints:
            EndpointRule.from_url(endpoint)
        return cls(
            mode=InternetAccessMode(mode),
            agent_name=agent_name,
            model_id=model_id,
            additional_allowed_endpoints=endpoints,
        )

    @property
    def is_filtered(self) -> bool:
        return self.mode == InternetAccessMode.FILTERED


@dataclass(frozen=True, order=True)
class EndpointRule:
    """One network destination that an evaluated agent can use."""

    host: str
    port: int
    path_prefix: str = "/"
    include_subpaths: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "host", _normalize_host(self.host))
        object.__setattr__(self, "path_prefix", _normalize_path(self.path_prefix))
        if not 1 <= self.port <= 65535:
            raise ValueError(f"Invalid endpoint port: {self.port}")

    @classmethod
    def from_url(cls, url: str) -> EndpointRule:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError(f"Endpoint must be an HTTP(S) URL: {url}")
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        return cls(
            host=_normalize_host(parsed.hostname),
            port=port,
            path_prefix=_normalize_path(parsed.path or "/"),
        )

    @classmethod
    def host_from_url(cls, url: str) -> EndpointRule:
        """Allow one HTTP(S) host and port, independent of its API path."""
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError(f"Endpoint must be an HTTP(S) URL: {url}")
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        return cls(host=parsed.hostname, port=port)

    @classmethod
    def from_dict(cls, value: dict) -> EndpointRule:
        include_subpaths = value.get("include_subpaths", True)
        if not isinstance(include_subpaths, bool):
            raise ValueError("include_subpaths must be a boolean")
        return cls(
            host=_normalize_host(str(value["host"])),
            port=int(value["port"]),
            path_prefix=_normalize_path(str(value.get("path_prefix", "/"))),
            include_subpaths=include_subpaths,
        )

    def to_dict(self) -> dict[str, str | int]:
        return {
            "host": self.host,
            "port": self.port,
            "path_prefix": self.path_prefix,
            "include_subpaths": self.include_subpaths,
        }

    def allows(self, host: str, port: int, request_target: str) -> bool:
        if _normalize_host(host) != self.host or port != self.port:
            return False
        path = _normalize_path(urlsplit(request_target).path or "/")
        if path == self.path_prefix:
            return True
        if not self.include_subpaths:
            return False
        if self.path_prefix == "/":
            return True
        return path.startswith(f"{self.path_prefix.rstrip('/')}/")


def endpoint_is_allowed(
    host: str,
    port: int,
    request_target: str,
    rules: tuple[EndpointRule, ...] | list[EndpointRule],
) -> bool:
    """Return whether a request matches one of the endpoint rules."""
    return any(rule.allows(host, port, request_target) for rule in rules)


def endpoint_host_is_allowed(
    host: str,
    port: int,
    rules: tuple[EndpointRule, ...] | list[EndpointRule],
) -> bool:
    """Return whether a CONNECT tunnel can lead to an allowed endpoint."""
    normalized_host = _normalize_host(host)
    return any(rule.host == normalized_host and rule.port == port for rule in rules)


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
    aliases = {
        "amazon-bedrock": "bedrock",
        "anthropic": "anthropic",
        "azure": "azure",
        "bedrock": "bedrock",
        "custom_openai": "custom",
        "deepseek": "deepseek",
        "gemini": "gemini",
        "google": "gemini",
        "google_ai_studio": "gemini",
        "groq": "groq",
        "huggingface": "huggingface",
        "mistral": "mistral",
        "moonshot": "moonshot",
        "ollama": "custom",
        "ollama_chat": "custom",
        "openai": "openai",
        "openrouter": "openrouter",
        "vertex_ai": "vertex",
        "watsonx": "watsonx",
        "xai": "xai",
        "zai": "zai-coding-plan",
        "zhipu": "zai-coding-plan",
    }
    if prefix in aliases and "/" in normalized:
        return aliases[prefix]
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
    if provider == "github-copilot":
        return _rules_from_urls(("https://api.githubcopilot.com",))
    if provider == "llama":
        return _rules_from_urls(("https://api.llama.com",))
    if provider == "opencode":
        return _rules_from_urls(("https://opencode.ai",))

    return _rules_from_urls((_provider_url(provider, environment),))


def _provider_url(provider: str, environment: Mapping[str, str]) -> str:
    configured_names = {
        "anthropic": ("ANTHROPIC_BASE_URL", "ANTHROPIC_API_BASE"),
        "deepseek": ("DEEPSEEK_API_BASE",),
        "gemini": ("GEMINI_API_BASE",),
        "groq": ("GROQ_API_BASE",),
        "huggingface": ("HF_ENDPOINT",),
        "mistral": ("MISTRAL_API_BASE",),
        "moonshot": ("MOONSHOT_API_BASE",),
        "openai": ("OPENAI_API_BASE", "OPENAI_BASE_URL"),
        "openrouter": ("OPENROUTER_API_BASE",),
        "xai": ("XAI_API_BASE",),
        "zai-coding-plan": ("ZAI_API_BASE",),
    }
    defaults = {
        "anthropic": "https://api.anthropic.com",
        "deepseek": "https://api.deepseek.com",
        "gemini": "https://generativelanguage.googleapis.com",
        "groq": "https://api.groq.com",
        "huggingface": "https://router.huggingface.co",
        "mistral": "https://api.mistral.ai",
        "moonshot": "https://api.moonshot.ai",
        "openai": "https://api.openai.com",
        "openrouter": "https://openrouter.ai",
        "xai": "https://api.x.ai",
        "zai-coding-plan": "https://api.z.ai",
    }
    if provider not in defaults:
        raise ValueError(
            f"Filtered internet access does not know the endpoint for provider '{provider}'. "
            "Set AGENT_API_BASE with a supported custom model or use --internet-access open."
        )
    return _configured_url(environment, *configured_names[provider]) or defaults[provider]


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


def provider_tool_uses_internet(request_body: bytes | str | None) -> bool:
    """Reject common provider-hosted web tools without scanning prompt text."""
    try:
        payload = json.loads(_body_text(request_body))
    except (TypeError, ValueError):
        return False
    if not isinstance(payload, dict):
        return False

    blocked_names = {
        "browser",
        "browser_agent",
        "code_interpreter",
        "computer",
        "computer_use",
        "computer_use_preview",
        "connector",
        "google_search",
        "google_search_retrieval",
        "mcp",
        "remote_mcp",
        "url_context",
        "web",
        "web_fetch",
        "web_search",
        "web_search_preview",
    }

    def is_blocked_tool(value: object) -> bool:
        if not isinstance(value, dict):
            return False
        for key, child in value.items():
            normalized_key = _normalize_tool_name(str(key))
            if normalized_key in blocked_names:
                return True
            if normalized_key in {"id", "name", "type"} and isinstance(child, str):
                normalized_value = _normalize_tool_name(child)
                if normalized_value in blocked_names or normalized_value.startswith(("web_fetch_", "web_search_")):
                    return True
        return False

    tools = payload.get("tools")
    plugins = payload.get("plugins")
    apps = payload.get("apps")
    mcp_servers = payload.get("mcp_servers")
    return (
        (isinstance(tools, list) and any(is_blocked_tool(tool) for tool in tools))
        or (isinstance(plugins, list) and bool(plugins))
        or (isinstance(apps, list) and bool(apps))
        or (isinstance(mcp_servers, list) and bool(mcp_servers))
    )


def should_stream_response(content_type: str | None) -> bool:
    """Return whether a response must bypass mitmproxy body buffering."""
    if not content_type:
        return False
    media_type = content_type.partition(";")[0].strip().casefold()
    return media_type == "text/event-stream"


def _decode_repeatedly(value: str, limit: int = 3) -> str:
    decoded = value
    for _ in range(limit):
        next_value = unquote(decoded)
        if next_value == decoded:
            break
        decoded = next_value
    return decoded


def _normalize_host(host: str) -> str:
    return host.strip().strip("[]").rstrip(".").casefold()


def _normalize_path(path: str) -> str:
    normalized: list[str] = []
    for segment in _decode_repeatedly(path).split("/"):
        if segment in {"", "."}:
            continue
        if segment == "..":
            if normalized:
                normalized.pop()
            continue
        normalized.append(segment)
    return f"/{'/'.join(normalized)}" if normalized else "/"


def _normalize_tool_name(value: str) -> str:
    snake_case = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", value)
    return re.sub(r"[^a-z0-9]+", "_", snake_case.casefold()).strip("_")


def _body_text(body: bytes | str | None, limit: int = 1_000_000) -> str:
    if body is None:
        return ""
    if isinstance(body, bytes):
        return body[:limit].decode("utf-8", errors="ignore")
    return body[:limit]
