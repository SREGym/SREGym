"""Internet-access policy shared by the agent runner and egress proxy."""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import unquote, urlsplit


class InternetAccessMode(StrEnum):
    OPEN = "open"
    FILTERED = "filtered"


DEFAULT_BLOCKED_GITHUB_OWNERS = ("SREGym",)
# The repository API accepts numeric IDs instead of an owner/repository path.
# Keep the benchmark repository protected when an agent uses that form.
DEFAULT_BLOCKED_GITHUB_REPOSITORY_IDS = ("986527564",)
GITHUB_CONTENT_HOSTS = {
    "api.github.com",
    "codeload.github.com",
    "github.com",
    "raw.githubusercontent.com",
}


@dataclass(frozen=True)
class InternetPolicy:
    mode: InternetAccessMode = InternetAccessMode.FILTERED
    blocked_github_owners: tuple[str, ...] = DEFAULT_BLOCKED_GITHUB_OWNERS
    blocked_github_repository_ids: tuple[str, ...] = DEFAULT_BLOCKED_GITHUB_REPOSITORY_IDS

    @classmethod
    def from_mode(cls, mode: str | InternetAccessMode) -> InternetPolicy:
        return cls(mode=InternetAccessMode(mode))

    @property
    def is_filtered(self) -> bool:
        return self.mode == InternetAccessMode.FILTERED


def blocked_github_owner(
    host: str,
    request_target: str,
    request_body: bytes | str | None,
    blocked_owners: tuple[str, ...] = DEFAULT_BLOCKED_GITHUB_OWNERS,
    blocked_repository_ids: tuple[str, ...] = DEFAULT_BLOCKED_GITHUB_REPOSITORY_IDS,
) -> str | None:
    """Return the blocked GitHub owner or repository route, if any."""
    normalized_host = host.partition(":")[0].rstrip(".").casefold()
    if normalized_host not in GITHUB_CONTENT_HOSTS:
        return None

    parsed = urlsplit(_decode_repeatedly(request_target))
    segments = _normalise_path_segments(parsed.path)
    query = _decode_repeatedly(parsed.query).casefold()
    body = _body_text(request_body).casefold()

    if (
        normalized_host == "api.github.com"
        and len(segments) > 1
        and segments[0] == "repositories"
        and segments[1] in {repository_id.casefold() for repository_id in blocked_repository_ids}
    ):
        return f"repository:{segments[1]}"

    for owner in blocked_owners:
        normalized_owner = owner.strip().casefold()
        if not normalized_owner:
            continue
        if _owner_is_in_path(normalized_host, segments, normalized_owner):
            return owner
        if normalized_owner in query or normalized_owner in body:
            return owner
        if normalized_host == "api.github.com" and segments and segments[0] == "graphql":
            if _decoded_json_contains(request_body, normalized_owner):
                return owner
    return None


def _owner_is_in_path(host: str, segments: tuple[str, ...], owner: str) -> bool:
    if not segments:
        return False
    if host in {"raw.githubusercontent.com", "codeload.github.com"}:
        return segments[0] == owner
    if host == "github.com":
        return segments[0] == owner or (len(segments) > 1 and segments[0] in {"orgs", "users"} and segments[1] == owner)
    if host == "api.github.com":
        return len(segments) > 1 and segments[0] in {"orgs", "repos", "users"} and segments[1] == owner
    return False


def _decode_repeatedly(value: str, limit: int = 3) -> str:
    decoded = value
    for _ in range(limit):
        next_value = unquote(decoded)
        if next_value == decoded:
            break
        decoded = next_value
    return decoded


def _normalise_path_segments(path: str) -> tuple[str, ...]:
    """Decode and resolve dot segments before applying path-based rules."""
    normalised: list[str] = []
    for raw_segment in path.split("/"):
        segment = _decode_repeatedly(raw_segment).casefold()
        if segment in {"", "."}:
            continue
        if segment == "..":
            if normalised:
                normalised.pop()
            continue
        normalised.append(segment)
    return tuple(normalised)


def _decoded_json_contains(body: bytes | str | None, needle: str) -> bool:
    """Match JSON string values after JSON escape processing."""
    try:
        decoded = json.loads(_body_text(body))
    except (TypeError, ValueError):
        return False
    return needle in json.dumps(decoded, ensure_ascii=False).casefold()


def _body_text(body: bytes | str | None, limit: int = 1_000_000) -> str:
    if body is None:
        return ""
    if isinstance(body, bytes):
        return body[:limit].decode("utf-8", errors="ignore")
    return body[:limit]
