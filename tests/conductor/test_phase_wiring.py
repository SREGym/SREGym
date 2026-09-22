"""The conductor's phase instrumentation.

Two properties matter beyond the ledger's own tests: an unbound ledger must not
change behaviour anywhere (cli.py, the external harness and most tests never
bind one), and a phase that raises must still leave a written-down end time --
that is the whole reason the ledger exists.
"""

import pytest

from sregym.conductor import conductor as conductor_mod
from sregym.conductor.conductor import Conductor
from sregym.phases import read_ledger, summarize


@pytest.fixture
def bare():
    """A Conductor with no cluster-touching __init__ and no ledger bound."""
    c = Conductor.__new__(Conductor)
    c.phases = None
    c.problem_id = "demo"
    c.logger = conductor_mod.logging.getLogger("test.phase_wiring")
    return c


def test_unbound_ledger_is_a_transparent_no_op(bare):
    with bare._phase("deploy"):
        pass
    bare._mark("stage:diagnosis", "start")  # must not raise either


def test_unbound_ledger_does_not_suppress_exceptions(bare):
    """A no-op context manager that swallowed would silently change control flow."""
    with pytest.raises(RuntimeError), bare._phase("deploy"):
        raise RuntimeError("boom")


def test_binding_stamps_problem_id_into_every_record(bare, tmp_path):
    bare.bind_phase_ledger(tmp_path / "phases.jsonl", attempt=2, agent="stratus")
    with bare._phase("deploy"):
        pass
    records = read_ledger(tmp_path / "phases.jsonl")
    assert records, "expected boundary records"
    for rec in records:
        assert rec["problem_id"] == "demo"
        assert rec["attempt"] == 2
        assert rec["agent"] == "stratus"


def test_a_failing_phase_is_recorded_then_reraised(bare, tmp_path):
    bare.bind_phase_ledger(tmp_path / "phases.jsonl")
    with pytest.raises(ValueError), bare._phase("deploy"):
        raise ValueError("deploy blew up")

    summary = summarize(read_ledger(tmp_path / "phases.jsonl"))
    assert summary["deploy"]["outcome"] == "error"
    assert "deploy blew up" in summary["deploy"]["error"]
    assert summary["deploy"]["end_ts"], "a failed phase still needs an end time"


def test_stage_boundaries_come_from_two_separate_call_sites(bare, tmp_path):
    """The agent stage opens in _advance_to_next_stage and closes at evaluation."""
    bare.bind_phase_ledger(tmp_path / "phases.jsonl")
    bare._mark("stage:diagnosis", "start")
    bare._mark("stage:diagnosis", "end", outcome="submitted")

    summary = summarize(read_ledger(tmp_path / "phases.jsonl"))
    assert summary["stage:diagnosis"]["outcome"] == "submitted"
    assert summary["stage:diagnosis"]["start_ts"]
    assert summary["stage:diagnosis"]["end_ts"]


def test_rebinding_per_attempt_keeps_ledgers_separate(bare, tmp_path):
    """Each attempt gets its own file rather than appending to a shared one."""
    for attempt in (1, 2):
        bare.bind_phase_ledger(tmp_path / f"phases_attempt{attempt}.jsonl", attempt=attempt)
        with bare._phase("deploy"):
            pass
    for attempt in (1, 2):
        records = read_ledger(tmp_path / f"phases_attempt{attempt}.jsonl")
        assert {r["attempt"] for r in records} == {attempt}


def test_a_stage_open_at_teardown_gets_an_end_record(bare, tmp_path):
    """An agent that exits without submitting still closes its stage.

    Otherwise the reader has to infer the end from the next phase's start,
    which is the synthesis the ledger exists to avoid.
    """
    bare.bind_phase_ledger(tmp_path / "phases.jsonl")
    bare.stage_sequence = [{"name": "diagnosis"}, {"name": "mitigation"}]
    bare._mark("stage:diagnosis", "start")
    # What _finish_problem does once it sees the stage still open.
    open_stage = "diagnosis"
    if open_stage in {s["name"] for s in bare.stage_sequence}:
        bare._mark(f"stage:{open_stage}", "end", outcome="no_submission")

    summary = summarize(read_ledger(tmp_path / "phases.jsonl"))
    assert summary["stage:diagnosis"]["outcome"] == "no_submission"
    assert summary["stage:diagnosis"]["end_ts"]


def test_a_cleanly_closed_stage_is_not_closed_twice(bare, tmp_path):
    """submission_stage still names the stage after it was evaluated.

    Closing on that alone wrote a second end and produced a phantom
    `stage:diagnosis#2` entry in the results.
    """
    bare.bind_phase_ledger(tmp_path / "phases.jsonl")
    bare.stage_sequence = [{"name": "diagnosis"}]
    bare._mark("stage:diagnosis", "start")
    bare._mark("stage:diagnosis", "end", outcome="submitted")

    assert not bare._phase_is_open("stage:diagnosis")
    # The teardown guard must now decline to close it again.
    if bare._phase_is_open("stage:diagnosis"):
        bare._mark("stage:diagnosis", "end", outcome="no_submission")

    summary = summarize(read_ledger(tmp_path / "phases.jsonl"))
    assert list(summary) == ["stage:diagnosis"], "no phantom #2 entry"
    assert summary["stage:diagnosis"]["outcome"] == "submitted"


def test_an_abandoned_stage_is_still_open_and_gets_closed(bare, tmp_path):
    bare.bind_phase_ledger(tmp_path / "phases.jsonl")
    bare.stage_sequence = [{"name": "mitigation"}]
    bare._mark("stage:mitigation", "start")

    assert bare._phase_is_open("stage:mitigation")
    bare._mark("stage:mitigation", "end", outcome="no_submission")

    summary = summarize(read_ledger(tmp_path / "phases.jsonl"))
    assert summary["stage:mitigation"]["outcome"] == "no_submission"
    assert summary["stage:mitigation"]["duration_s"] is not None


def test_phase_is_open_is_false_without_a_ledger(bare):
    bare.phases = None
    assert bare._phase_is_open("stage:diagnosis") is False
