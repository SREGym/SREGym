"""Explicit character-level parsers for every textual shape the verifier trusts.

The grading path has no regular expressions. A regex is a compact way to write a
parser and a terrible way to review one: a shipped grader's acceptance set has to
be readable line by line by whoever is deciding whether a verdict is fair, and
``re`` hides that set behind an engine with its own escaping, Unicode, and
backtracking rules. Two of those rules had already leaked into grading semantics
here — ``\\w``/``\\s`` are Unicode-aware while the adjacent explicit classes like
``[A-Za-z0-9]`` are ASCII-only, so the same manifest could be canonicalized two
different ways depending on which validator saw it first.

Every predicate below is total and side-effect free: it answers yes/no about a
``str`` and never raises. Parsers that can fail return ``None`` and leave the
loud error to the caller, which is the one that knows what evidence went wrong.

ASCII is meant literally throughout. ``str.isalnum``/``str.isdigit`` are Unicode
aware and are deliberately NOT used: ``"٣"`` is a digit to Python and must not be
a digit to a grader.
"""

from __future__ import annotations

ASCII_DIGITS = frozenset("0123456789")
ASCII_LOWER = frozenset("abcdefghijklmnopqrstuvwxyz")
ASCII_UPPER = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
ASCII_LETTERS = ASCII_LOWER | ASCII_UPPER
ASCII_ALNUM = ASCII_LETTERS | ASCII_DIGITS
LOWER_HEX = ASCII_DIGITS | frozenset("abcdef")

_IDENTIFIER_TAIL = ASCII_ALNUM | frozenset("_.-")
_RELATIVE_PATH_TAIL = ASCII_ALNUM | frozenset("_./-")
_DOTTED_NAME_HEAD = ASCII_LETTERS | frozenset("_")
_DOTTED_NAME_TAIL = ASCII_ALNUM | frozenset("_.")


def _is_str(value: object) -> bool:
    return isinstance(value, str)


def is_lower_hex(value: object, length: int) -> bool:
    """``value`` is exactly ``length`` lowercase hex digits."""
    return (
        _is_str(value)
        and len(value) == length
        and all(char in LOWER_HEX for char in value)
    )


def is_hex_digest(value: object) -> bool:
    """``value`` is a bare lowercase sha256 digest (64 hex digits)."""
    return is_lower_hex(value, 64)


def is_prefixed_hex_digest(value: object) -> bool:
    """``value`` is ``sha256:`` followed by a bare lowercase sha256 digest."""
    return (
        _is_str(value)
        and value.startswith("sha256:")
        and is_hex_digest(value[len("sha256:") :])
    )


def is_sentinel(value: object, *, prefix: str, hex_length: int) -> bool:
    """``value`` is ``prefix`` followed by exactly ``hex_length`` lowercase hex digits."""
    return (
        _is_str(value)
        and value.startswith(prefix)
        and is_lower_hex(value[len(prefix) :], hex_length)
    )


def is_challenge_id(value: object) -> bool:
    """``value`` is ``bundle-`` plus the first 24 hex digits of a bundle digest."""
    return is_sentinel(value, prefix="bundle-", hex_length=24)


def is_identifier(value: object) -> bool:
    """A contract/check/envelope/channel ID: ASCII alnum start, then alnum ``_.-``."""
    return (
        _is_str(value)
        and value != ""
        and value[0] in ASCII_ALNUM
        and all(char in _IDENTIFIER_TAIL for char in value[1:])
    )


def is_relative_slash_path(value: object) -> bool:
    """A task-verifier relative path: ASCII alnum start, then alnum ``_./-``.

    Structural path safety (leading ``/``, ``..`` segments, ``//``, trailing
    ``/``) is a separate concern and stays with the caller, which is where the
    error message belongs.
    """
    return (
        _is_str(value)
        and value != ""
        and value[0] in ASCII_ALNUM
        and all(char in _RELATIVE_PATH_TAIL for char in value[1:])
    )


def is_dotted_name(value: object) -> bool:
    """A Postgres setting name: letter or ``_`` start, then alnum ``_.``."""
    return (
        _is_str(value)
        and value != ""
        and value[0] in _DOTTED_NAME_HEAD
        and all(char in _DOTTED_NAME_TAIL for char in value[1:])
    )


# --------------------------------------------------------------------------- #
# Durations
# --------------------------------------------------------------------------- #
DURATION_SCALE_MS: dict[str | None, int] = {
    None: 1,
    "ms": 1,
    "s": 1000,
    "min": 60_000,
    "h": 3_600_000,
}


def parse_duration_ms(raw: object) -> int | None:
    """Parse a Postgres-style ``<digits><unit?>`` duration into milliseconds.

    Returns ``None`` for anything that is not exactly digits plus one of the
    recognized units. A bare number is milliseconds, matching Postgres' own
    default for the timeout settings this reads.
    """
    if not _is_str(raw):
        return None
    text = raw.strip()
    digits = 0
    while digits < len(text) and text[digits] in ASCII_DIGITS:
        digits += 1
    if digits == 0:
        return None
    unit: str | None = text[digits:] or None
    if unit not in DURATION_SCALE_MS:
        return None
    return int(text[:digits]) * DURATION_SCALE_MS[unit]


# --------------------------------------------------------------------------- #
# Container image digests
# --------------------------------------------------------------------------- #
def find_image_digest(text: object) -> str | None:
    """The bare digest of the first ``sha256:<64 hex>`` token in ``text``.

    A token is only accepted when it is not followed by a further hex digit, so
    a 65-hex-digit blob is rejected rather than silently truncated to 64. Kubelet
    writes ``imageID`` in several shapes (``docker-pullable://repo@sha256:...``,
    a bare ``sha256:...``), so this scans rather than anchoring.
    """
    if not _is_str(text):
        return None
    marker = "sha256:"
    start = text.find(marker)
    while start != -1:
        digest = text[start + len(marker) : start + len(marker) + 64]
        after = start + len(marker) + 64
        if is_hex_digest(digest) and (
            after == len(text) or text[after] not in LOWER_HEX
        ):
            return digest
        start = text.find(marker, start + 1)
    return None


def pinned_image_digest(reference: object) -> str | None:
    """The bare digest of a fully pinned ``<name>@sha256:<64 hex>`` reference.

    ``<name>`` must be non-empty and the digest must end the reference, so a
    floating tag or a digest with trailing content is not a pin.
    """
    if not _is_str(reference):
        return None
    marker = "@sha256:"
    at = reference.rfind(marker)
    if at <= 0:
        return None
    digest = reference[at + len(marker) :]
    return digest if is_hex_digest(digest) else None


# --------------------------------------------------------------------------- #
# Secret-bearing field names
# --------------------------------------------------------------------------- #
# A deterministic report is published evidence, so a field whose NAME advertises
# a credential is refused outright. Tokens match on ``_``/``-``/boundary edges
# only, which is why "secrets" is not a hit while "app_secret" is.
_SECRET_TOKENS = (
    "authorization",
    "credential",
    "password",
    "secret",
    "token",
    "apikey",
    "api_key",
    "api-key",
)
_SECRET_DELIMITERS = frozenset("_-")


def names_a_secret(key: object) -> bool:
    """``key`` contains a delimiter-bounded secret word."""
    if not _is_str(key):
        return False
    folded = key.lower()
    for token in _SECRET_TOKENS:
        start = folded.find(token)
        while start != -1:
            end = start + len(token)
            left_ok = start == 0 or folded[start - 1] in _SECRET_DELIMITERS
            right_ok = end == len(folded) or folded[end] in _SECRET_DELIMITERS
            if left_ok and right_ok:
                return True
            start = folded.find(token, start + 1)
    return False


# --------------------------------------------------------------------------- #
# Prometheus text exposition
# --------------------------------------------------------------------------- #
_METRIC_NAME_HEAD = ASCII_LETTERS | frozenset("_:")
_METRIC_NAME_TAIL = ASCII_ALNUM | frozenset("_:")
_LABEL_NAME_HEAD = ASCII_LETTERS | frozenset("_")
_LABEL_NAME_TAIL = ASCII_ALNUM | frozenset("_")


def _parse_number(text: str) -> float | None:
    """Parse a Prometheus sample value, which must consume all of ``text``."""
    index = 0
    if index < len(text) and text[index] in "+-":
        index += 1
    integer_digits = 0
    while index < len(text) and text[index] in ASCII_DIGITS:
        index += 1
        integer_digits += 1
    fraction_digits = 0
    if index < len(text) and text[index] == ".":
        index += 1
        while index < len(text) and text[index] in ASCII_DIGITS:
            index += 1
            fraction_digits += 1
    if integer_digits == 0 and fraction_digits == 0:
        return None
    if index < len(text) and text[index] in "eE":
        index += 1
        if index < len(text) and text[index] in "+-":
            index += 1
        exponent_digits = 0
        while index < len(text) and text[index] in ASCII_DIGITS:
            index += 1
            exponent_digits += 1
        if exponent_digits == 0:
            return None
    if index != len(text):
        return None
    return float(text)


def parse_metric_line(line: str) -> tuple[str, str, float] | None:
    """Split one exposition line into ``(name, raw_labels, value)``.

    Returns ``None`` for any line that is not a sample — a comment, a blank, a
    line carrying a trailing timestamp — because a scrape legitimately contains
    those and the caller skips them. A line that IS shaped like a sample but
    carries a broken label block still parses here; ``parse_metric_labels``
    is what refuses it loudly.
    """
    index = 0
    if index >= len(line) or line[index] not in _METRIC_NAME_HEAD:
        return None
    index += 1
    while index < len(line) and line[index] in _METRIC_NAME_TAIL:
        index += 1
    name = line[:index]
    labels = ""
    if index < len(line) and line[index] == "{":
        close = line.find("}", index)
        if close == -1:
            return None
        labels = line[index + 1 : close]
        index = close + 1
    separator = index
    while index < len(line) and line[index].isspace():
        index += 1
    if index == separator:
        return None
    value = _parse_number(line[index:])
    if value is None:
        return None
    return name, labels, value


def parse_metric_labels(raw: str) -> dict[str, str] | None:
    """Parse ``a="1",b="2"`` into a mapping, or ``None`` if it is malformed.

    Label values are backslash-escaped per the exposition format, so a comma or
    a quote inside a value is handled by position rather than by splitting. A
    repeated label name is malformed, not last-wins: two values for one series
    dimension means the scrape cannot be interpreted.
    """
    labels: dict[str, str] = {}
    if raw == "":
        return labels
    index = 0
    while True:
        start = index
        if index >= len(raw) or raw[index] not in _LABEL_NAME_HEAD:
            return None
        index += 1
        while index < len(raw) and raw[index] in _LABEL_NAME_TAIL:
            index += 1
        name = raw[start:index]
        if index + 1 >= len(raw) or raw[index] != "=" or raw[index + 1] != '"':
            return None
        index += 2
        chunks: list[str] = []
        while True:
            if index >= len(raw):
                return None
            char = raw[index]
            if char == '"':
                index += 1
                break
            if char == "\\":
                if index + 1 >= len(raw):
                    return None
                chunks.append(raw[index : index + 2])
                index += 2
                continue
            chunks.append(char)
            index += 1
        if name in labels:
            return None
        # Exposition escapes are C-style; decode them the same way the
        # exposition producers encode them.
        labels[name] = bytes("".join(chunks), "utf-8").decode("unicode_escape")
        if index == len(raw):
            return labels
        if raw[index] != ",":
            return None
        index += 1
