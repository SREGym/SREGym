"""Internet-access policy shared by the agent runner and egress proxy."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import unquote, urlsplit


class InternetAccessMode(StrEnum):
    OPEN = "open"
    FILTERED = "filtered"


DEFAULT_BLOCKED_GITHUB_OWNERS = ("SREGym",)
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
) -> str | None:
    """Return the blocked GitHub owner referenced by a request, if any."""
    normalized_host = host.partition(":")[0].rstrip(".").casefold()
    if normalized_host not in GITHUB_CONTENT_HOSTS:
        return None

    parsed = urlsplit(_decode_repeatedly(request_target))
    segments = tuple(_decode_repeatedly(segment).casefold() for segment in parsed.path.split("/") if segment)
    query = _decode_repeatedly(parsed.query).casefold()
    body = _body_text(request_body).casefold()

    for owner in blocked_owners:
        normalized_owner = owner.strip().casefold()
        if not normalized_owner:
            continue
        if _owner_is_in_path(normalized_host, segments, normalized_owner):
            return owner
        if normalized_owner in query or normalized_owner in body:
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


def _body_text(body: bytes | str | None, limit: int = 1_000_000) -> str:
    if body is None:
        return ""
    if isinstance(body, bytes):
        return body[:limit].decode("utf-8", errors="ignore")
    return body[:limit]
