"""Token totals for client results. This module has no ATIF dependency.

Input includes cache reads and writes. Output includes reasoning. The cache
and reasoning fields are breakdowns, not additional tokens. Version 2 marks
these definitions; older results did not use consistent definitions.
"""

import json
import logging
from collections.abc import Iterable, Mapping
from pathlib import Path

logger = logging.getLogger(__name__)
TOKEN_METRICS_VERSION = 2
TOKEN_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cached_input_tokens",
    "cache_creation_input_tokens",
    "reasoning_output_tokens",
)


def token_count(value: object) -> int | None:
    """Keep reported nonnegative integer counts, including explicit zero."""
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def sum_counts(values: Iterable[int | None]) -> int | None:
    """Sum reported counts without turning an absent measurement into zero."""
    known = [value for value in values if value is not None]
    return sum(known) if known else None


def usage_metrics(
    *,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    cached_input_tokens: int | None = None,
    cache_creation_input_tokens: int | None = None,
    reasoning_output_tokens: int | None = None,
) -> dict[str, int | None]:
    """Build results from inclusive totals. Do not add the breakdowns again."""
    return {
        "token_metrics_version": TOKEN_METRICS_VERSION,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cached_input_tokens": cached_input_tokens,
        "cache_creation_input_tokens": cache_creation_input_tokens,
        "reasoning_output_tokens": reasoning_output_tokens,
        "total_tokens": input_tokens + output_tokens
        if input_tokens is not None and output_tokens is not None
        else None,
    }


def aggregate_usage(records: Iterable[Mapping[str, int | None]]) -> dict[str, int | None]:
    """Sum distinct calls. Callers must resolve cumulative or repeated records."""
    records = list(records)
    return usage_metrics(**{field: sum_counts(record.get(field) for record in records) for field in TOKEN_FIELDS})


def read_jsonl(path: Path) -> Iterable[dict]:
    """Read complete records, including records from interrupted runs."""
    try:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(record, dict):
                    yield record
    except OSError as error:
        logger.warning("Could not read token usage from %s: %s", path, error)
