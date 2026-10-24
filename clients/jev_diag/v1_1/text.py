"""Shortening of messages for display, shared by the modules that collect and describe platform events and logs."""

from __future__ import annotations

import re

MAX_MESSAGE_CHARS = 240
_WS_RE = re.compile(r"\s+")
_LONG_HEX = re.compile(r"\b[0-9a-f]{24,}\b")  # sandbox, container and digest ids: no meaning to a reader


def trim(text: str | None, limit: int = MAX_MESSAGE_CHARS) -> str:
    """Whitespace-collapsed text of at most `limit` characters. A longer text first gives up long hexadecimal ids
    (sandbox, container and digest ids), then keeps its head, and also its last clauses when it is one error chain
    and they are short: a chain is a single sentence that joins its causes with ": ", innermost last ("dial tcp ...:
    connect: connection refused"), so its head says what failed and its last clauses say why. A message of several
    sentences is not a chain; its head is kept."""
    text = _WS_RE.sub(" ", (text or "").strip())
    if len(text) <= limit:
        return text
    text = _LONG_HEX.sub("…", text)  # a too-long message gives up its ids before any of its words
    if len(text) <= limit:
        return text
    parts, tail = text.split(": "), ""
    for k in range(1, len(parts)):
        clauses = ": ".join(parts[-k:])
        if len(clauses) > limit // 3:
            break
        tail = clauses
    if tail and ". " not in text[: len(text) - len(tail)]:
        return f"{text[: limit - len(tail) - 3].rstrip()} … {tail}"
    return text[: limit - 1] + "…"
