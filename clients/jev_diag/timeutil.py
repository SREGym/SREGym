"""Time helpers shared by the collector and the derivations.

`now()` can be pinned to a fixed instant so a recorded run can be replayed offline with the ages it had when it
was collected (see collector.set_kubectl_replay).
"""

from __future__ import annotations

from datetime import UTC, datetime

_NOW_OVERRIDE: datetime | None = None


def now() -> datetime:
    return _NOW_OVERRIDE or datetime.now(UTC)


def set_now(value: datetime | None) -> None:
    global _NOW_OVERRIDE
    _NOW_OVERRIDE = value


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def seconds_between(later: datetime | None, earlier: datetime | None) -> int | None:
    if later is None or earlier is None:
        return None
    return int((later - earlier).total_seconds())


def seconds_ago(value: datetime | str | None) -> int | None:
    moment = parse_time(value) if isinstance(value, str) else value
    if moment is None:
        return None
    return max(0, int((now() - moment).total_seconds()))
