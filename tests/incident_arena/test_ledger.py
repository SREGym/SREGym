import json
import subprocess
import sys
from pathlib import Path

from sregym.generators.workload import incident_arena_ledger as ledger


def _record(sent_s, phase="c1.peak", ok=True, correct=True, latency=10.0, driver="write", **extra):
    return {
        "phase": phase,
        "sent_s": sent_s,
        "latency_ms": latency,
        "ok": ok,
        "correct": correct,
        "timeout": False,
        "dropped": False,
        "driver": driver,
        **extra,
    }


def test_percentile_is_nearest_rank():
    assert ledger.percentile([], 99) is None
    assert ledger.percentile([5, 1, 3], 50) == 3
    assert ledger.percentile(list(range(1, 101)), 99) == 99
    assert ledger.percentile(list(range(1, 101)), 90) == 90


def test_phase_kind():
    assert ledger.phase_kind("c12.peak") == "peak"
    assert ledger.phase_kind("soak.trough") == "trough"
    assert ledger.phase_kind("warmup") is None


def test_summary_windows_and_rates():
    records = [
        _record(1.0, ok=False),  # before the mark: excluded
        _record(11.0),
        _record(12.0, ok=False),
        _record(13.0, ok=False, timeout=True),
        _record(14.0, correct=False, phase="c2.trough", latency=50.0),
        _record(15.0, dropped=True),
    ]
    summary = ledger.summarize(records, since_s=10.0)
    assert summary["offered"] == 4
    assert summary["dropped"] == 1
    # error rate counts timeouts and non-ok responses over non-dropped arrivals
    assert summary["failures"] == 2
    assert summary["error_rate"] == 0.5
    # goodput counts ok AND correct
    assert summary["good"] == 1
    assert summary["goodput_ratio"] == 0.25
    assert summary["latency"]["peak"]["n"] == 3
    assert summary["latency"]["trough"]["p_ms"] == 50.0
    assert summary["first_sent_s"] == 11.0 and summary["last_sent_s"] == 15.0


def test_settle_window_only_drops_latency_samples():
    records = [_record(11.0, latency=999.0), _record(50.0, latency=5.0)]
    summary = ledger.summarize(records, since_s=10.0, latency_percentile=99.0, settle_s=30.0)
    assert summary["offered"] == 2
    assert summary["latency"]["peak"] == {"n": 1, "p_ms": 5.0}


def test_by_driver_breakdown():
    records = [_record(1.0, driver="rq_enqueue"), _record(2.0, driver="rq_complete", ok=False)]
    summary = ledger.summarize(records)
    assert summary["by_driver"]["rq_enqueue"]["error_rate"] == 0.0
    assert summary["by_driver"]["rq_complete"]["error_rate"] == 1.0


def test_parse_skips_summary_rows_and_torn_lines():
    lines = [json.dumps(_record(1.0)), '{"summary": true, "offered": 1}', '{"phase": "c1.pe', ""]
    assert len(list(ledger.parse_records(lines))) == 1


def test_latest_sent_s_reads_the_tail(tmp_path):
    path = tmp_path / "loadgen.jsonl"
    assert ledger.latest_sent_s(str(path)) is None
    path.write_text("\n".join(json.dumps(_record(float(i))) for i in range(5000)) + "\n")
    assert ledger.latest_sent_s(str(path), tail_bytes=4096) == 4999.0


def test_script_runs_standalone_like_in_the_pod(tmp_path):
    """SREGym pipes the module into the load generator pod as `python3 - <mode>`."""
    (tmp_path / "loadgen.jsonl").write_text(json.dumps(_record(3.5)) + "\n")
    source = Path(ledger.__file__).read_text()
    env = {"GRADER_DIR": str(tmp_path), "PATH": "/usr/bin:/bin"}
    latest = subprocess.run(
        [sys.executable, "-", "latest"], input=source, capture_output=True, text=True, env=env, check=True
    )
    assert json.loads(latest.stdout) == {"latest_sent_s": 3.5}
    summary = subprocess.run(
        [sys.executable, "-", "summary", "none", "90", "0"],
        input=source,
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )
    assert json.loads(summary.stdout)["offered"] == 1
