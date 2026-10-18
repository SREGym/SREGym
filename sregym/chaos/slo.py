"""End-user SLOs measured from the wrk2 workload generator.

Every wrk2 round (one ``WorkloadEntry``) is parsed into a ``RoundStats``. A
round meets the SLO when its error ratio, tail latency and delivered
throughput are all within bounds. The bounds are absolute floors widened by a
multiple of the healthy baseline, so a fault has to hurt users noticeably, not
just move a percentile within normal jitter.
"""

from __future__ import annotations

import math
import re
import time
from contextlib import suppress
from dataclasses import asdict, dataclass, field

_UNITS_MS = {"us": 0.001, "ms": 1.0, "s": 1000.0, "m": 60_000.0, "h": 3_600_000.0}

_REQUESTS = re.compile(r"^\s*(\d+) requests in ([\d.]+)(us|ms|s|m|h)")
_NON2XX = re.compile(r"Non-2xx or 3xx responses:\s*(\d+)")
_SOCKET = re.compile(r"Socket errors: connect (\d+), read (\d+), write (\d+), timeout (\d+)")
_PERCENTILE = re.compile(r"^\s*(50|90|99)\.0+%\s+([\d.]+)(us|ms|s|m|h)\s*$")


@dataclass
class RoundStats:
    time: float
    requests: int
    duration_s: float
    non2xx: int = 0
    connect_errors: int = 0
    read_errors: int = 0
    write_errors: int = 0
    timeouts: int = 0
    p50_ms: float | None = None
    p90_ms: float | None = None
    p99_ms: float | None = None

    @property
    def errors(self) -> int:
        return self.non2xx + self.connect_errors + self.read_errors + self.write_errors

    @property
    def attempted(self) -> int:
        return max(self.requests + self.connect_errors, 1)

    @property
    def error_ratio(self) -> float:
        return min(self.errors / self.attempted, 1.0)

    @property
    def timeout_ratio(self) -> float:
        # wrk2 counts requests still in flight when a round ends as timeouts, so
        # healthy rounds report a few percent; judge them against the baseline.
        return min(self.timeouts / self.attempted, 1.0)

    @property
    def rps(self) -> float:
        return self.requests / self.duration_s if self.duration_s > 0 else 0.0


def _to_ms(value: str, unit: str) -> float:
    return float(value) * _UNITS_MS[unit]


def parse_round(entry) -> RoundStats | None:
    """Parse one wrk2 round from a ``WorkloadEntry``; None if it is not a full round."""
    stats = None
    percentiles: dict[str, float] = {}
    non2xx = 0
    socket = (0, 0, 0, 0)
    for line in entry.log.splitlines():
        if match := _REQUESTS.match(line):
            stats = (int(match.group(1)), _to_ms(match.group(2), match.group(3)) / 1000)
        elif match := _NON2XX.search(line):
            non2xx = int(match.group(1))
        elif match := _SOCKET.search(line):
            socket = tuple(int(x) for x in match.groups())
        elif match := _PERCENTILE.match(line):
            # The first match of each percentile is the latency distribution;
            # the HdrHistogram dump that follows uses a different format.
            percentiles.setdefault(match.group(1), _to_ms(match.group(2), match.group(3)))
    if stats is None:
        return None
    return RoundStats(
        time=entry.time,
        requests=stats[0],
        duration_s=stats[1],
        non2xx=non2xx,
        connect_errors=socket[0],
        read_errors=socket[1],
        write_errors=socket[2],
        timeouts=socket[3],
        # Short rounds can end with an empty histogram, printed as 0.00us.
        p50_ms=percentiles.get("50") or None,
        p90_ms=percentiles.get("90") or None,
        p99_ms=percentiles.get("99") or None,
    )


@dataclass
class SLO:
    """Thresholds a healthy round must meet. ``target_rps`` is the wrk2 rate."""

    target_rps: float
    max_error_ratio: float = 0.05
    p99_floor_ms: float = 1000.0
    p99_baseline_multiplier: float = 5.0
    min_throughput_fraction: float = 0.7
    timeout_floor: float = 0.15
    timeout_margin: float = 0.10
    max_timeout_ratio: float = 0.25
    max_p99_ms: float = 5000.0
    baseline_p99_ms: float | None = None
    baseline_timeout_ratio: float = 0.0
    baseline_error_ratio: float = 0.0

    @property
    def p99_limit_ms(self) -> float:
        if self.baseline_p99_ms is None:
            return self.p99_floor_ms
        return min(self.max_p99_ms, max(self.p99_floor_ms, self.p99_baseline_multiplier * self.baseline_p99_ms))

    @property
    def error_limit(self) -> float:
        return self.max_error_ratio

    @property
    def timeout_limit(self) -> float:
        return min(self.max_timeout_ratio, max(self.timeout_floor, self.baseline_timeout_ratio + self.timeout_margin))

    def violations(self, stats: RoundStats) -> list[str]:
        out = []
        if stats.error_ratio > self.error_limit:
            out.append(f"error_ratio={stats.error_ratio:.3f}>{self.error_limit:.3f}")
        if stats.timeout_ratio > self.timeout_limit:
            out.append(f"timeout_ratio={stats.timeout_ratio:.3f}>{self.timeout_limit:.3f}")
        if stats.p99_ms is not None and stats.p99_ms > self.p99_limit_ms:
            out.append(f"p99={stats.p99_ms:.0f}ms>{self.p99_limit_ms:.0f}ms")
        if stats.rps < self.min_throughput_fraction * self.target_rps:
            out.append(f"rps={stats.rps:.1f}<{self.min_throughput_fraction * self.target_rps:.1f}")
        return out

    def to_dict(self) -> dict:
        return {
            **asdict(self),
            "p99_limit_ms": self.p99_limit_ms,
            "timeout_limit": self.timeout_limit,
            "error_limit": self.error_limit,
        }


@dataclass
class WindowVerdict:
    rounds: int
    violating: int
    healthy: bool  # sustained: at most isolated, non-adjacent noisy rounds (see judge)
    violated: bool  # most rounds, including the latest, missed the SLO
    measurement_complete: bool
    details: list[dict] = field(default_factory=list)
    coverage: dict | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class MeasurementWindow:
    """Collected rounds and the observation interval they must cover."""

    rounds: list[RoundStats]
    start: float
    duration_s: float
    round_interval_s: float
    elapsed_s: float

    def coverage(self) -> dict:
        end = self.start + self.duration_s
        # Excluding an in-progress round permits one round at the leading edge.
        # Elsewhere, allow the generator's one-second pause and bounded jitter,
        # not another missing round. Shorter rounds get a smaller allowance.
        jitter_s = min(5.0, self.round_interval_s * 0.1)
        leading_allowance_s = self.round_interval_s + jitter_s
        gap_allowance_s = 1.0 + jitter_s
        valid = [
            r
            for r in self.rounds
            if math.isfinite(r.time)
            and math.isfinite(r.duration_s)
            and r.duration_s > 0
            and self.start <= r.time <= end
        ]
        valid.sort(key=lambda r: r.time)
        leading = valid[0].time - self.start if valid else self.duration_s
        cursor = self.start
        largest_gap = 0.0
        overlap = 0.0
        for index, stats in enumerate(valid):
            if index:
                largest_gap = max(largest_gap, stats.time - cursor)
                overlap = max(overlap, cursor - stats.time)
            cursor = max(cursor, stats.time + stats.duration_s)
        trailing = max(0.0, end - cursor)
        complete = (
            bool(valid)
            and len(valid) == len(self.rounds)
            and self.elapsed_s >= self.duration_s
            and leading <= leading_allowance_s
            and trailing <= gap_allowance_s
            and largest_gap <= gap_allowance_s
            and overlap <= jitter_s
        )
        return {
            "complete": complete,
            "requested_s": self.duration_s,
            "elapsed_s": round(self.elapsed_s, 3),
            "leading_gap_s": round(leading, 3),
            "trailing_gap_s": round(trailing, 3),
            "largest_gap_s": round(largest_gap, 3),
            "largest_overlap_s": round(overlap, 3),
            "leading_allowance_s": round(leading_allowance_s, 3),
            "gap_allowance_s": round(gap_allowance_s, 3),
        }


def judge(
    slo: SLO,
    rounds: list[RoundStats] | MeasurementWindow,
    violation_fraction: float = 0.6,
    noise_fraction: float = 0.1,
) -> WindowVerdict:
    """Judge supplied rounds; collected windows also require temporal coverage."""
    coverage = rounds.coverage() if isinstance(rounds, MeasurementWindow) else None
    if isinstance(rounds, MeasurementWindow):
        rounds = sorted(rounds.rounds, key=lambda stats: stats.time)
    details = []
    violating = 0
    for stats in rounds:
        problems = slo.violations(stats)
        violating += bool(problems)
        details.append(
            {
                "time": stats.time,
                "requests": stats.requests,
                "rps": round(stats.rps, 1),
                "error_ratio": round(stats.error_ratio, 4),
                "timeout_ratio": round(stats.timeout_ratio, 4),
                "p99_ms": stats.p99_ms,
                "violations": problems,
            }
        )
    n = len(rounds)
    # wrk2 rounds (10s for social-network) occasionally report one p99 spike on a healthy system, so a
    # window stays healthy with a few isolated bad rounds; adjacent bad rounds mean real degradation.
    flags = [bool(d["violations"]) for d in details]
    adjacent = any(a and b for a, b in zip(flags, flags[1:], strict=False))
    allowed = max(1, int(n * noise_fraction))
    measurement_complete = (
        n > 0 and all(r.p99_ms is not None for r in rounds) and (coverage is None or coverage["complete"])
    )
    return WindowVerdict(
        rounds=n,
        violating=violating,
        healthy=measurement_complete and violating <= allowed and not adjacent,
        violated=n > 0 and violating / n >= violation_fraction and bool(details[-1]["violations"]),
        measurement_complete=measurement_complete,
        details=details,
        coverage=coverage,
    )


def target_rps(app) -> float:
    return float(app.wrk.wrk.rate)


def round_seconds(app) -> float:
    return float(app.wrk.wrk.duration) + 1.0


def collect_rounds(app, window_s: float, min_rounds: int = 1, timeout_s: float | None = None) -> MeasurementWindow:
    """Wait for wrk2 rounds that *start* in the next ``window_s`` seconds.

    Rounds already in progress when called are excluded, so the result only
    reflects the system after this call.
    """
    wrk = app.wrk
    with suppress(Exception):
        wrk._extractlog()
    # Entry times are UTC epoch seconds, independent of the host timezone.
    started = time.time()
    started_monotonic = time.monotonic()
    interval_s = round_seconds(app)
    timeout = timeout_s if timeout_s is not None else window_s + 3 * interval_s + 60
    deadline = started_monotonic + timeout
    window_end_wall = started + window_s
    out: list[RoundStats] = []
    while time.monotonic() < deadline:
        time.sleep(5)
        try:
            wrk._extractlog()
        except Exception:
            continue
        out = [s for e in wrk.log_history if started <= e.time <= window_end_wall and (s := parse_round(e)) is not None]
        if time.monotonic() >= started_monotonic + window_s + interval_s and len(out) >= min_rounds:
            break
    return MeasurementWindow(
        rounds=out,
        start=started,
        duration_s=window_s,
        round_interval_s=interval_s,
        elapsed_s=time.monotonic() - started_monotonic,
    )


def recent_rounds(app, duration_s: float) -> list[RoundStats]:
    """Rounds that ended within the last ``duration_s`` seconds (non-blocking)."""
    try:
        entries = app.wrk.recent_entries(duration=duration_s)
    except Exception:  # no rounds yet, or the generator pod is restarting
        return []
    return [s for e in entries if (s := parse_round(e)) is not None]
