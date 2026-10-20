"""Summaries of the tenant workload ledger.

The ledger holds one record per completed tenant operation: when it started
(``t``), when it ended, whether it succeeded and why not. A window counts the
operations that *started* inside it; callers wait for the workload's longest
operation timeout after a window closes, so every such operation has ended.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field


@dataclass
class OpStats:
    attempts: int = 0
    successes: int = 0
    latencies: list[float] = field(default_factory=list)
    errors: dict[str, int] = field(default_factory=dict)

    @property
    def success_ratio(self) -> float | None:
        return self.successes / self.attempts if self.attempts else None

    def p(self, quantile: float) -> float | None:
        if not self.latencies:
            return None
        ordered = sorted(self.latencies)
        return ordered[min(len(ordered) - 1, max(0, math.ceil(quantile * len(ordered)) - 1))]

    def as_dict(self) -> dict:
        ratio = self.success_ratio
        return {
            "attempts": self.attempts,
            "successes": self.successes,
            "success_ratio": None if ratio is None else round(ratio, 4),
            "p50_s": self.p(0.5),
            "p95_s": self.p(0.95),
            "errors": dict(sorted(self.errors.items(), key=lambda kv: -kv[1])[:5]),
        }


def error_class(message: str | None) -> str:
    """Collapse an error message to a stable class (ids and numbers dropped)."""
    if not message:
        return "unknown"
    text = (
        message.split(":")[0]
        if message.startswith(("boot rejected", "reboot rejected", "delete rejected"))
        else message
    )
    words = [w for w in text.replace("=", " ").split() if not any(ch.isdigit() for ch in w)]
    return " ".join(words[:8]) or "unknown"


def summarize(records: list[dict], since: float, until: float) -> dict:
    """Return {"by_op": {op: stats}, "by_tenant": {tenant: {op: stats}}} for one window."""
    by_op: dict[str, OpStats] = defaultdict(OpStats)
    by_tenant: dict[str, dict[str, OpStats]] = defaultdict(lambda: defaultdict(OpStats))
    for record in records:
        if not since <= record["t"] <= until:
            continue
        for stats in (by_op[record["op"]], by_tenant[record["tenant"]][record["op"]]):
            stats.attempts += 1
            stats.latencies.append(record["latency_s"])
            if record["ok"]:
                stats.successes += 1
            else:
                cls = error_class(record.get("error"))
                stats.errors[cls] = stats.errors.get(cls, 0) + 1
    return {
        "since": since,
        "until": until,
        "by_op": {op: stats.as_dict() for op, stats in sorted(by_op.items())},
        "by_tenant": {
            tenant: {op: stats.as_dict() for op, stats in sorted(ops.items())}
            for tenant, ops in sorted(by_tenant.items())
        },
    }


@dataclass(frozen=True)
class SLO:
    """Minimum success ratio per operation, and how many attempts make a window conclusive."""

    min_success: dict[str, float]
    min_attempts: dict[str, int]
    #: Per-tenant floor for operations on a tenant's own resident servers, so a
    #: failure concentrated on one customer is not averaged away.
    tenant_min_success: dict[str, float] = field(default_factory=dict)


def violations(summary: dict, slo: SLO) -> list[str]:
    out = []
    for op, minimum in slo.min_success.items():
        stats = summary["by_op"].get(op)
        attempts = stats["attempts"] if stats else 0
        if attempts < slo.min_attempts.get(op, 1):
            out.append(f"{op}: {attempts} attempts < {slo.min_attempts.get(op, 1)} required")
            continue
        if stats["success_ratio"] < minimum:
            out.append(f"{op}: success {stats['success_ratio']:.3f} < {minimum:.3f} ({stats['errors']})")
    for tenant, ops in summary["by_tenant"].items():
        for op, minimum in slo.tenant_min_success.items():
            stats = ops.get(op)
            if stats and stats["attempts"] and stats["success_ratio"] < minimum:
                out.append(f"{tenant} {op}: success {stats['success_ratio']:.3f} < {minimum:.3f} ({stats['errors']})")
    return out
