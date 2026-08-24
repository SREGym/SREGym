"""Internet-access policy shared by the agent runner and egress proxy."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import parse_qsl, unquote, urlsplit


class InternetAccessMode(StrEnum):
    OPEN = "open"
    FILTERED = "filtered"


DEFAULT_BLOCKED_GITHUB_OWNERS = ("SREGym",)
DEFAULT_BLOCKED_GITHUB_REPOSITORIES = ("SREGym",)
# The repository API accepts numeric IDs instead of an owner/repository path.
# Keep the benchmark repository protected when an agent uses that form.
DEFAULT_BLOCKED_GITHUB_REPOSITORY_IDS = ("986527564",)
GITHUB_CONTENT_HOSTS = {
    "api.github.com",
    "codeload.github.com",
    "cdn.jsdelivr.net",
    "github.com",
    "raw.githubusercontent.com",
    "raw.githack.com",
}
GITHUB_MIRROR_HOSTS = {"cdn.jsdelivr.net", "raw.githack.com"}


@dataclass(frozen=True)
class InternetPolicy:
    mode: InternetAccessMode = InternetAccessMode.FILTERED
    blocked_github_owners: tuple[str, ...] = DEFAULT_BLOCKED_GITHUB_OWNERS
    blocked_github_repository_ids: tuple[str, ...] = DEFAULT_BLOCKED_GITHUB_REPOSITORY_IDS
    blocked_github_repositories: tuple[str, ...] = DEFAULT_BLOCKED_GITHUB_REPOSITORIES

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
    blocked_repositories: tuple[str, ...] = DEFAULT_BLOCKED_GITHUB_REPOSITORIES,
) -> str | None:
    """Return the blocked GitHub owner or repository route, if any."""
    normalized_host = host.partition(":")[0].rstrip(".").casefold()
    if normalized_host not in GITHUB_CONTENT_HOSTS:
        return None

    parsed = urlsplit(_decode_repeatedly(request_target))
    segments = _normalise_path_segments(parsed.path)
    query = _decode_repeatedly(parsed.query).casefold()

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
        if _query_references_owner(query, normalized_owner):
            return owner
        if normalized_host == "api.github.com" and segments and segments[0] == "graphql":
            if _decoded_json_references_owner(request_body, normalized_owner):
                return owner

    repository = _repository_in_path(normalized_host, segments, blocked_repositories)
    if repository is not None:
        return f"repository:{repository}"
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
    if host == "cdn.jsdelivr.net":
        return len(segments) > 2 and segments[0] == "gh" and segments[1] == owner
    if host == "raw.githack.com":
        return len(segments) > 1 and segments[0] == owner
    return False


def _repository_in_path(host: str, segments: tuple[str, ...], repositories: tuple[str, ...]) -> str | None:
    blocked = {repository.strip().casefold() for repository in repositories if repository.strip()}
    if not blocked:
        return None

    repository_segment: str | None = None
    if host in {"github.com", "raw.githubusercontent.com", "codeload.github.com"} and len(segments) > 1:
        repository_segment = segments[1]
    elif host == "api.github.com" and len(segments) > 2 and segments[0] in {"orgs", "repos"}:
        repository_segment = segments[2]
    elif host in GITHUB_MIRROR_HOSTS:
        # jsDelivr uses /gh/{owner}/{repo}@{ref}/..., while raw.githack uses
        # /{owner}/{repo}/{ref}/....
        if host == "cdn.jsdelivr.net" and len(segments) > 2 and segments[0] == "gh":
            repository_segment = segments[2].split("@", 1)[0]
        elif len(segments) > 1:
            repository_segment = segments[1]

    if repository_segment in blocked:
        return repository_segment
    return None


def _query_references_owner(query: str, owner: str) -> bool:
    for key, value in parse_qsl(_decode_repeatedly(query), keep_blank_values=True):
        key = key.casefold()
        value = _decode_repeatedly(value).casefold()
        if key in {"owner", "organization", "org"} and value == owner:
            return True
        if key in {"q", "query"} and re.search(rf"(?:^|\s)(?:org|user|repo):{re.escape(owner)}(?:\b|/)", value):
            return True
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


def _decoded_json_references_owner(body: bytes | str | None, needle: str) -> bool:
    """Match GraphQL owner/repository fields after JSON escape processing."""
    try:
        decoded = json.loads(_body_text(body))
    except (TypeError, ValueError):
        return False

    def references(value: object, key: str = "") -> bool:
        if isinstance(value, dict):
            return any(references(child_value, str(child_key).casefold()) for child_key, child_value in value.items())
        if isinstance(value, list):
            return any(references(item, key) for item in value)
        if not isinstance(value, str):
            return False
        normalized = value.casefold()
        if key in {"owner", "repositoryowner", "repository_owner", "organization", "org"}:
            return normalized == needle
        if key in {"repo", "repository"}:
            return normalized == needle or normalized.startswith(f"{needle}/")
        if key in {"q", "query"}:
            return bool(re.search(rf"(?:^|\s)(?:org|user|repo):{re.escape(needle)}(?:\b|/)", normalized))
        return False

    return references(decoded)


def _body_text(body: bytes | str | None, limit: int = 1_000_000) -> str:
    if body is None:
        return ""
    if isinstance(body, bytes):
        return body[:limit].decode("utf-8", errors="ignore")
    return body[:limit]
