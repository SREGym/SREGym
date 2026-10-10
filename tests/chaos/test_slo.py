from sregym.chaos.slo import SLO, judge, parse_round
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
    healthy = parse_round(entry(ROUND.replace("  Non-2xx or 3xx responses: 290\n", "").replace("1.20s", "20.00ms")
                                .replace("timeout 10", "timeout 0")))
    assert slo.violations(healthy) == []
    slow = parse_round(entry(ROUND.replace("2990 requests", "1000 requests")))
    assert any(v.startswith("rps") for v in SLO(target_rps=100).violations(slow))
    verdict = judge(slo, [healthy, stats, stats])
    assert verdict.violated and not verdict.healthy and verdict.violating == 2
    assert judge(slo, [healthy, healthy]).healthy


def test_incomplete_round_is_ignored():
    assert parse_round(entry("Running 30s test @ http://x")) is None


def test_baseline_timeouts_are_tolerated():
    noisy = parse_round(entry(ROUND.replace("  Non-2xx or 3xx responses: 290\n", "").replace("1.20s", "20.00ms")
                              .replace("timeout 10", "timeout 186")))
    slo = SLO(target_rps=100, baseline_timeout_ratio=0.06)
    assert slo.violations(noisy) == []
    hung = parse_round(entry(ROUND.replace("  Non-2xx or 3xx responses: 290\n", "").replace("1.20s", "20.00ms")
                             .replace("timeout 10", "timeout 900")))
    assert any(v.startswith("timeout_ratio") for v in slo.violations(hung))
