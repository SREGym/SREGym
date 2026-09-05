"""Internet-access policy shared by the agent runner and egress proxy."""

from __future__ import annotations

import json
import re
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


def provider_tool_uses_internet(request_body: bytes | str | None) -> bool:
    """Reject common provider-hosted web tools without scanning prompt text."""
    try:
        # json.loads accepts UTF-8/16/32 bytes, just as JSON API servers can.
        payload = json.loads(request_body)
    except RecursionError:
        return True
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

    def uses_internet(request: dict) -> bool:
        tools = request.get("tools")
        if isinstance(tools, list) and any(is_blocked_tool(tool) for tool in tools):
            return True
        if any(request.get(key) for key in ("plugins", "apps", "mcp_servers")):
            return True
        # WebSocket APIs can wrap a request in a response/session event.
        # Do not traverse messages, prompts, or function argument schemas.
        return any(
            uses_internet(value)
            for key in ("response", "session", "request")
            if isinstance(value := request.get(key), dict)
        )

    try:
        return uses_internet(payload)
    except RecursionError:
        return True


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
