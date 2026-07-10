"""Shared helpers for ATIF trace adapters.

These functions are tool-agnostic and every adapter shares the same behavior for the mechanical parts:
JSON-safe stringification, NDJSON loading with skip-on-error, and per-step
metric summation into ``FinalMetrics``.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from ..atif import FinalMetrics, Step

logger = logging.getLogger(__name__)


def _stringify(value: Any) -> str:
    """Return a string representation of any value.

    Strings pass through unchanged; other values are JSON-serialized (preserving
    unicode) with a ``str()`` fallback when JSON can't represent the value.
    """
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False)
    except TypeError:
        return str(value)


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    """Read an NDJSON file into a list of parsed dicts.

    Blank lines are skipped. Malformed JSON lines and OS read errors are logged
    at debug level and skipped — adapters must never raise on bad input.
    """
    records: list[dict[str, Any]] = []
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    records.append(json.loads(stripped))
                except json.JSONDecodeError as exc:
                    logger.debug("Skipping malformed JSONL line in %s: %s", path, exc)
    except OSError as exc:
        logger.debug("Skipping unreadable file %s: %s", path, exc)
    return records


def _aggregate_final_metrics(
    steps: list[Step],
    *,
    total_cost_usd: float | None = None,
    extra: dict[str, Any] | None = None,
) -> FinalMetrics:
    """Sum per-step ``Metrics`` into a ``FinalMetrics`` aggregate.

    ``prompt_tokens``, ``completion_tokens``, and ``cached_tokens`` are summed
    across all steps that carry them. ``total_cost_usd`` is taken from the
    caller (e.g. parsed from a stream file or supplied as ``None``) since
    per-step cost is unreliable across tools. ``total_steps`` is the step count.
    """
    prompt_values = [s.metrics.prompt_tokens for s in steps if s.metrics and s.metrics.prompt_tokens is not None]
    completion_values = [
        s.metrics.completion_tokens for s in steps if s.metrics and s.metrics.completion_tokens is not None
    ]
    cached_values = [s.metrics.cached_tokens for s in steps if s.metrics and s.metrics.cached_tokens is not None]

    return FinalMetrics(
        total_prompt_tokens=sum(prompt_values) if prompt_values else None,
        total_completion_tokens=sum(completion_values) if completion_values else None,
        total_cached_tokens=sum(cached_values) if cached_values else None,
        total_cost_usd=total_cost_usd,
        total_steps=len(steps),
        extra=extra,
    )
