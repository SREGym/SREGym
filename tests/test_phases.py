"""The phase ledger's value is that it survives the run not surviving.

So the cases that matter are the ugly ones: a phase that raises, a process that
dies mid-phase, a truncated final line, and a host that suspends.
"""

import json
import time

import pytest

from sregym import phases


@pytest.fixture
def ledger(tmp_path):
    return phases.PhaseLedger(tmp_path / "phases.jsonl", context={"problem_id": "demo"})


def test_a_clean_phase_records_both_boundaries(ledger):
    with ledger.phase("deploy"):
        pass
    records = phases.read_ledger(ledger.path)
    assert [r["event"] for r in records] == ["start", "end"]
    assert records[0]["phase"] == records[1]["phase"] == "deploy"
    assert records[1]["outcome"] == "ok"
    # Context is stamped on every record so lines stand alone.
    assert all(r["problem_id"] == "demo" for r in records)


def test_both_clocks_and_an_iso_timestamp_are_recorded(ledger):
    with ledger.phase("deploy"):
        pass
    for rec in phases.read_ledger(ledger.path):
        assert isinstance(rec["wall"], float)
        assert isinstance(rec["mono"], float)
        assert rec["ts"].endswith("Z")


def test_a_raising_phase_still_gets_an_end_time(ledger):
    """The whole point: no synthesising end times for failed phases."""
    with pytest.raises(RuntimeError, match="boom"), ledger.phase("deploy"):
        raise RuntimeError("boom")

    records = phases.read_ledger(ledger.path)
    assert [r["event"] for r in records] == ["start", "end"]
    end = records[1]
    assert end["outcome"] == "error"
    assert "RuntimeError: boom" in end["error"]
    assert end["duration_s"] >= 0


def test_the_exception_is_not_swallowed(ledger):
    with pytest.raises(KeyError), ledger.phase("deploy"):
        raise KeyError("k")


def test_keyboard_interrupt_is_recorded_and_propagated(ledger):
    """BaseException, not Exception -- a Ctrl-C run should still be measurable."""
    with pytest.raises(KeyboardInterrupt), ledger.phase("agent"):
        raise KeyboardInterrupt
    end = phases.read_ledger(ledger.path)[1]
    assert end["outcome"] == "error"
    assert "KeyboardInterrupt" in end["error"]


def test_a_phase_with_no_end_is_reported_incomplete(ledger):
    """Simulates the process dying inside a phase."""
    ledger.record("deploy", "start")
    summary = phases.summarize(phases.read_ledger(ledger.path))
    assert summary["deploy"]["outcome"] == "incomplete"
    assert summary["deploy"].get("duration_s") is None
    assert summary["deploy"]["start_ts"]  # the start is still usable


def test_a_truncated_final_line_is_ignored(ledger):
    with ledger.phase("deploy"):
        pass
    with open(ledger.path, "a", encoding="utf-8") as fh:
        fh.write('{"phase": "cleanup", "event": "sta')  # killed mid-write
    records = phases.read_ledger(ledger.path)
    assert len(records) == 2
    assert phases.summarize(records)["deploy"]["outcome"] == "ok"


def test_repeated_phases_stay_distinguishable(ledger):
    for _ in range(2):
        with ledger.phase("deploy"):
            pass
    summary = phases.summarize(phases.read_ledger(ledger.path))
    assert set(summary) == {"deploy", "deploy#2"}


def test_a_suspend_is_flagged_not_absorbed(ledger, monkeypatch):
    """Wall jumps, monotonic does not: the host was not executing."""
    walls = iter([1000.0, 1000.0, 1900.0, 1900.0])
    monos = iter([50.0, 50.0, 52.0, 52.0])
    monkeypatch.setattr(phases.time, "time", lambda: next(walls))
    monkeypatch.setattr(phases.time, "monotonic", lambda: next(monos))

    with ledger.phase("agent"):
        pass

    end = phases.read_ledger(ledger.path)[1]
    assert end["duration_s"] == pytest.approx(2.0)  # real work
    assert end["wall_duration_s"] == pytest.approx(900.0)  # elapsed clock
    assert end["suspended_s"] == pytest.approx(898.0)
    assert phases.suspended_seconds(phases.read_ledger(ledger.path)) == pytest.approx(898.0)


def test_no_suspend_flag_under_normal_jitter(ledger):
    with ledger.phase("quick"):
        time.sleep(0.01)
    end = phases.read_ledger(ledger.path)[1]
    assert "suspended_s" not in end


def test_results_columns_are_narrow_when_healthy(ledger):
    with ledger.phase("deploy"):
        pass
    cols = phases.results_columns(phases.read_ledger(ledger.path))
    assert list(cols) == ["phase.deploy.duration_s"]


def test_results_columns_name_the_broken_phase(ledger):
    with pytest.raises(RuntimeError), ledger.phase("deploy"):
        raise RuntimeError("nope")
    cols = phases.results_columns(phases.read_ledger(ledger.path))
    assert cols["phase.deploy.outcome"] == "error"
    assert "phase.deploy.duration_s" in cols


def test_an_unwritable_ledger_does_not_break_the_run(tmp_path, monkeypatch):
    target = tmp_path / "nested" / "phases.jsonl"
    monkeypatch.setattr(phases.Path, "mkdir", lambda *a, **k: (_ for _ in ()).throw(OSError("read-only")))
    led = phases.PhaseLedger(target)
    with led.phase("deploy"):  # must not raise
        pass
    assert not target.exists()


def test_records_are_durable_without_closing_the_ledger(ledger):
    """Each record is flushed and fsynced, so a SIGKILL loses nothing prior."""
    ledger.record("deploy", "start")
    # Read through a separate handle while the ledger is still "in use".
    with open(ledger.path, encoding="utf-8") as fh:
        assert json.loads(fh.readline())["phase"] == "deploy"


def test_a_manually_marked_phase_still_gets_a_duration(ledger):
    """Stages open and close from two different call sites, not a `with`.

    They must still carry duration_s, or the reader is back to subtracting
    timestamps by hand.
    """
    ledger.record("stage:diagnosis", "start")
    time.sleep(0.01)
    ledger.record("stage:diagnosis", "end", outcome="submitted")

    end = phases.read_ledger(ledger.path)[1]
    assert end["duration_s"] > 0
    assert end["wall_duration_s"] > 0
    assert end["outcome"] == "submitted"

    summary = phases.summarize(phases.read_ledger(ledger.path))
    assert summary["stage:diagnosis"]["duration_s"] > 0


def test_an_end_without_a_start_has_the_same_entry_shape(ledger):
    """The teardown close can fire for a stage whose start was never recorded."""
    ledger.record("stage:mitigation", "end", outcome="no_submission")
    entry = phases.summarize(phases.read_ledger(ledger.path))["stage:mitigation"]
    # Present-but-None, so consumers can index it uniformly.
    assert entry["start_ts"] is None
    assert entry["outcome"] == "no_submission"
    assert entry["end_ts"]


def test_context_manager_durations_win_over_the_tracker(ledger):
    """Both paths compute a duration; the `with` block's value is authoritative."""
    with ledger.phase("deploy"):
        time.sleep(0.01)
    end = phases.read_ledger(ledger.path)[1]
    assert end["duration_s"] > 0
    # One start, one end, no duplicate bookkeeping.
    assert len(phases.read_ledger(ledger.path)) == 2
