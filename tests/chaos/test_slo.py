from types import SimpleNamespace

import pytest

from sregym.chaos.slo import SLO, MeasurementWindow, RoundStats, collect_rounds, judge, parse_round
from sregym.generators.workload.base import WorkloadEntry

ROUND = """Running 30s test @ http://10.96.0.10:5000
  3 threads and 100 connections
  Thread Stats   Avg      Stdev     Max   +/- Stdev
    Latency     5.12ms    3.21ms  50.00ms   80.00%
  Latency Distribution (HdrHistogram - Recorded Latency)
 50.000%    4.50ms
 75.000%    6.00ms
 90.000%    8.00ms
 99.000%    1.20s
 99.900%    2.00s

  Detailed Percentile spectrum:
       Value   Percentile   TotalCount 1/(1-Percentile)
       4.500     0.500000         1495         2.00
----------------------------------------------------------
  2990 requests in 30.00s, 1.20MB read
  Socket errors: connect 0, read 0, write 0, timeout 10
  Non-2xx or 3xx responses: 290
Requests/sec:     99.67
Transfer/sec:     40.00KB"""


def entry(log, t=1.0):
    return WorkloadEntry(time=t, number=0, log=log, ok=True)


def test_parse_round():
    stats = parse_round(entry(ROUND))
    assert stats.requests == 2990
    assert stats.duration_s == 30.0
    assert stats.non2xx == 290 and stats.timeouts == 10
    assert stats.p50_ms == 4.5 and stats.p90_ms == 8.0 and stats.p99_ms == 1200.0
    assert abs(stats.error_ratio - 290 / 2990) < 1e-9
    assert abs(stats.timeout_ratio - 10 / 2990) < 1e-9


def test_judge_flags_errors_latency_and_throughput():
    stats = parse_round(entry(ROUND))
    slo = SLO(target_rps=100, baseline_p99_ms=100)
    assert any(v.startswith("error_ratio") for v in slo.violations(stats))
    assert slo.p99_limit_ms == 1000
    assert any(v.startswith("p99") for v in slo.violations(stats))
    healthy = parse_round(
        entry(
            ROUND.replace("  Non-2xx or 3xx responses: 290\n", "")
            .replace("1.20s", "20.00ms")
            .replace("timeout 10", "timeout 0")
        )
    )
    assert slo.violations(healthy) == []
    slow = parse_round(entry(ROUND.replace("2990 requests", "1000 requests")))
    assert any(v.startswith("rps") for v in SLO(target_rps=100).violations(slow))
    verdict = judge(slo, [healthy, stats, stats])
    assert verdict.violated and not verdict.healthy and verdict.violating == 2
    assert judge(slo, [healthy, healthy]).healthy
    # One isolated spike in a sustained window is wrk2 noise; adjacent or frequent ones are not.
    assert judge(slo, [healthy] * 8 + [stats] + [healthy] * 8).healthy
    assert not judge(slo, [healthy] * 8 + [stats, stats] + [healthy] * 8).healthy
    assert not judge(slo, [healthy, stats, healthy, stats, healthy, healthy]).healthy


def test_incomplete_round_is_ignored():
    assert parse_round(entry("Running 30s test @ http://x")) is None


def test_missing_latency_is_not_a_healthy_measurement():
    missing = parse_round(entry(ROUND.replace("1.20s", "0.00us")))
    assert missing.p99_ms is None
    verdict = judge(SLO(target_rps=100), [missing] * 5)
    assert not verdict.measurement_complete
    assert not verdict.healthy


def test_measurement_selects_only_rounds_inside_its_window(monkeypatch):
    from sregym.chaos import slo

    clock = SimpleNamespace(now=100.0)
    monkeypatch.setattr(slo.time, "time", lambda: clock.now)
    monkeypatch.setattr(slo.time, "monotonic", lambda: clock.now)
    monkeypatch.setattr(slo.time, "sleep", lambda seconds: setattr(clock, "now", clock.now + seconds))
    workload = SimpleNamespace(
        wrk=SimpleNamespace(duration=10),
        log_history=[entry(ROUND, t) for t in [99, 100, 120, 130, 131, 18000]],
        _extractlog=lambda: None,
    )
    window = collect_rounds(SimpleNamespace(wrk=workload), window_s=30, min_rounds=3)
    assert [r.time for r in window.rounds] == [100, 120, 130]
    assert window.start == 100
    assert window.duration_s == 30


def measured_window(times, duration_s=180, round_s=30, elapsed_s=None):
    rounds = [RoundStats(time=t, requests=100 * round_s, duration_s=round_s, p99_ms=10) for t in times]
    return MeasurementWindow(rounds, 0, duration_s, round_s + 1, duration_s if elapsed_s is None else elapsed_s)


@pytest.mark.parametrize("round_s", [1, 5, 10, 30, 60])
@pytest.mark.parametrize("phase", [0, 0.5, 0.99])
def test_continuous_window_allows_round_alignment_and_small_jitter(round_s, phase):
    interval = round_s + 1
    duration_s = interval * 10
    times = [t for i in range(10) if (t := phase * interval + i * (interval + 0.01)) <= duration_s]
    window = measured_window(times, duration_s, round_s)
    verdict = judge(SLO(target_rps=100), window)
    assert verdict.healthy
    assert verdict.measurement_complete
    assert verdict.coverage["complete"]


@pytest.mark.parametrize(
    "times",
    [
        [],
        [0, 31, 62, 93],  # Missing tail.
        [62, 93, 124, 155],  # Missing beginning.
        [0, 31, 93, 124, 155],  # Missing interior round.
        [0, 31, 62, 62, 93, 124, 155],  # Duplicate round.
    ],
)
def test_incomplete_window_cannot_pass(times):
    verdict = judge(SLO(target_rps=100), measured_window(times))
    assert not verdict.healthy
    assert not verdict.measurement_complete
    assert not verdict.coverage["complete"]


def test_clock_or_timeout_cannot_finish_observation_early():
    window = measured_window([0, 31, 62, 93, 124, 155], elapsed_s=100)
    assert not judge(SLO(target_rps=100), window).measurement_complete


@pytest.mark.parametrize("duration", [0, -30, float("inf"), float("nan")])
def test_invalid_round_duration_does_not_supply_coverage(duration):
    window = measured_window([0, 31, 62, 93, 124, 155])
    window.rounds[2].duration_s = duration
    assert not judge(SLO(target_rps=100), window).measurement_complete


def test_collection_retains_missing_tail_evidence(monkeypatch):
    from sregym.chaos import slo

    clock = SimpleNamespace(now=1000.0)
    monkeypatch.setattr(slo.time, "time", lambda: clock.now)
    monkeypatch.setattr(slo.time, "monotonic", lambda: clock.now)
    monkeypatch.setattr(slo.time, "sleep", lambda seconds: setattr(clock, "now", clock.now + seconds))
    healthy = ROUND.replace("  Non-2xx or 3xx responses: 290\n", "").replace("1.20s", "20.00ms")
    workload = SimpleNamespace(
        wrk=SimpleNamespace(duration=30),
        log_history=[entry(healthy, t) for t in [1001, 1032, 1063, 1094]],
        _extractlog=lambda: None,
    )
    window = collect_rounds(SimpleNamespace(wrk=workload), window_s=513, min_rounds=4)
    verdict = judge(SLO(target_rps=100), window)
    assert clock.now == 1545
    assert len(window.rounds) == 4
    assert not verdict.healthy
    assert verdict.coverage["trailing_gap_s"] == 389


def test_zero_collection_timeout_returns_an_incomplete_window(monkeypatch):
    workload = SimpleNamespace(wrk=SimpleNamespace(duration=30), _extractlog=lambda: None, log_history=[])
    window = collect_rounds(SimpleNamespace(wrk=workload), window_s=90, timeout_s=0)
    assert not judge(SLO(target_rps=100), window).measurement_complete


def test_baseline_timeouts_are_tolerated():
    noisy = parse_round(
        entry(
            ROUND.replace("  Non-2xx or 3xx responses: 290\n", "")
            .replace("1.20s", "20.00ms")
            .replace("timeout 10", "timeout 186")
        )
    )
    slo = SLO(target_rps=100, baseline_timeout_ratio=0.06)
    assert slo.violations(noisy) == []
    hung = parse_round(
        entry(
            ROUND.replace("  Non-2xx or 3xx responses: 290\n", "")
            .replace("1.20s", "20.00ms")
            .replace("timeout 10", "timeout 900")
        )
    )
    assert any(v.startswith("timeout_ratio") for v in slo.violations(hung))


def test_calibration_never_relaxes_absolute_ceilings():
    policy = SLO(target_rps=100, baseline_error_ratio=1, baseline_timeout_ratio=1, baseline_p99_ms=10000)
    assert policy.error_limit == 0.05
    assert policy.timeout_limit == 0.25
    assert policy.p99_limit_ms == 5000


@pytest.mark.parametrize("errors,expected", [(0, False), (49, False), (50, False), (51, True), (1000, True)])
def test_absolute_error_budget_boundary(errors, expected):
    stats = parse_round(
        entry(ROUND.replace("2990 requests", "1000 requests").replace("290", str(errors)).replace("1.20s", "20.00ms"))
    )
    assert any(v.startswith("error_ratio") for v in SLO(target_rps=30).violations(stats)) is expected


def test_log_surges():
    from sregym.chaos.telemetry import log_surges

    before = {"frontend": 10.0, "search": 400.0}
    after = {"frontend": 200.0, "search": 500.0, "rate": 5.0}
    assert log_surges(before, after) == {"frontend": [10.0, 200.0]}


def test_cpu_surges():
    from sregym.chaos.telemetry import cpu_surges

    before = {"compose-post-service": 0.1, "nginx": 0.5, "redis": 0.01}
    after = {"compose-post-service": 0.9, "nginx": 0.7, "redis": 0.05}
    assert cpu_surges(before, after) == {"compose-post-service": [0.1, 0.9]}
