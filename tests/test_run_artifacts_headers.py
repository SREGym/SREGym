"""Header stability across a run's per-attempt result CSVs.

finalize_and_publish() writes each attempt's row against whatever fieldnames
main.py's driver had accumulated by that point in the run, so a file
published early in a run is narrower than one published once a later
attempt's oracle has contributed a key of its own.

stabilize_run_csv_headers() reconciles every already-published per-attempt
CSV under a run directory to one shared header, called once the run's
attempts are all in.

Fixture headers are written pre-sorted (main.py's own `sorted(...)` order),
matching what finalize_and_publish() actually produces, so a test failure
means a real content difference rather than an incidental column reorder.
"""

import csv
from pathlib import Path

from sregym.run_artifacts import stabilize_run_csv_headers


def _publish(root: Path, agent: str, problem_id: str, attempt: int, text: str) -> Path:
    """A per-attempt CSV at the path finalize_and_publish() actually uses."""
    run_dir = root / agent / problem_id / f"run_{attempt}"
    run_dir.mkdir(parents=True)
    path = run_dir / f"{problem_id}_results.csv"
    path.write_text(text)
    return path


def _header(path: Path) -> list[str]:
    with path.open(newline="") as fh:
        return next(csv.reader(fh))


def _rows(path: Path) -> list[dict]:
    with path.open(newline="") as fh:
        return list(csv.DictReader(fh))


def test_header_stays_identical_across_files_even_when_only_some_rows_produce_a_key(tmp_path):
    """The exact mechanism: one problem's oracle contributes a key another
    attempt's never does, so an early file's header should not stay narrower
    than a later file's."""
    early = _publish(tmp_path, "agent", "p1", 0, "TTL,attempt,problem_id\n12.5,0,p1\n")
    later = _publish(
        tmp_path,
        "agent",
        "p2",
        0,
        "Mitigation.internalTrafficPolicy,TTL,attempt,problem_id\nLocal,9.0,0,p2\n",
    )

    result = stabilize_run_csv_headers(tmp_path)

    assert set(result.rewritten) == {early}  # later already had the full header
    assert result.overflow_dropped == []
    assert _header(early) == _header(later)
    assert _header(early) == ["Mitigation.internalTrafficPolicy", "TTL", "attempt", "problem_id"]


def test_genuinely_absent_value_is_empty_distinct_from_written_false(tmp_path):
    """Mitigation.success missing (the harness killed the agent before the
    oracle ran) must not read the same as Mitigation.success recorded False
    (the oracle ran and failed)."""
    no_verdict = _publish(tmp_path, "agent", "p1", 0, "attempt,problem_id,timed_out\n0,p1,True\n")
    failed = _publish(tmp_path, "agent", "p2", 0, "Mitigation.success,attempt,problem_id\nFalse,0,p2\n")

    stabilize_run_csv_headers(tmp_path)

    no_verdict_row = _rows(no_verdict)[0]
    failed_row = _rows(failed)[0]
    assert no_verdict_row["Mitigation.success"] == ""  # never ran
    assert failed_row["Mitigation.success"] == "False"  # ran, failed
    assert no_verdict_row["Mitigation.success"] != failed_row["Mitigation.success"]


def test_full_union_lands_in_every_file(tmp_path):
    """Every file ends up with the same header: the union of every column
    that appeared anywhere under the run directory."""
    a = _publish(tmp_path, "agent", "p1", 0, "Diagnosis.success,attempt,problem_id\nTrue,0,p1\n")
    b = _publish(tmp_path, "agent", "p1", 1, "TTM,attempt,problem_id\n42,1,p1\n")
    c = _publish(
        tmp_path,
        "agent",
        "p2",
        0,
        "agent_timeout_seconds,attempt,problem_id,timed_out\n1800,0,p2,True\n",
    )

    stabilize_run_csv_headers(tmp_path)

    expected = ["Diagnosis.success", "TTM", "agent_timeout_seconds", "attempt", "problem_id", "timed_out"]
    for path in (a, b, c):
        assert _header(path) == expected

    # And no data was lost or shifted in the rewrite.
    assert _rows(a)[0]["Diagnosis.success"] == "True"
    assert _rows(a)[0]["TTM"] == ""
    assert _rows(b)[0]["TTM"] == "42"
    assert _rows(c)[0]["timed_out"] == "True"


def test_already_uniform_files_are_left_untouched(tmp_path):
    """A run where every attempt happened to produce the same keys should not
    be rewritten at all."""
    a = _publish(tmp_path, "agent", "p1", 0, "attempt,problem_id\n0,p1\n")
    b = _publish(tmp_path, "agent", "p2", 0, "attempt,problem_id\n0,p2\n")

    result = stabilize_run_csv_headers(tmp_path)
    assert result.rewritten == []
    assert result.overflow_dropped == []
    assert _header(a) == ["attempt", "problem_id"]
    assert _header(b) == ["attempt", "problem_id"]


def test_fewer_than_two_files_is_a_no_op(tmp_path):
    """Nothing to reconcile with only one (or zero) published files."""
    _publish(tmp_path, "agent", "p1", 0, "attempt,problem_id\n0,p1\n")

    assert stabilize_run_csv_headers(tmp_path).rewritten == []
    assert stabilize_run_csv_headers(tmp_path / "nothing-here").rewritten == []


def test_only_the_files_whose_header_actually_changed_are_reported(tmp_path):
    """A caller logging progress should not be told about files that were
    already correct."""
    already_full = _publish(tmp_path, "agent", "p1", 0, "Mitigation.success,attempt,problem_id\nTrue,0,p1\n")
    narrower = _publish(tmp_path, "agent", "p2", 0, "attempt,problem_id\n0,p2\n")

    result = stabilize_run_csv_headers(tmp_path)

    assert result.rewritten == [narrower]
    assert result.overflow_dropped == []
    assert _header(already_full) == ["Mitigation.success", "attempt", "problem_id"]


def test_rollup_and_per_problem_aggregate_files_are_not_touched(tmp_path):
    """Neither of the two other CSVs living in a run tree is a per-attempt
    file, and sweeping either into the union rewrite would change what they
    mean, not just widen a header."""
    narrow = _publish(tmp_path, "agent", "p1", 0, "attempt,problem_id\n0,p1\n")
    _publish(tmp_path, "agent", "p2", 0, "Mitigation.success,attempt,problem_id\nTrue,0,p2\n")

    rollup = tmp_path / "agent_ALL_results.csv"
    rollup.write_text("attempt,deploy_failed,problem_id\n1,True,p3\n")
    rollup_before = rollup.read_text()

    aggregate = tmp_path / "agent" / "p1" / "p1_agent_results.csv"
    aggregate.parent.mkdir(parents=True, exist_ok=True)
    aggregate.write_text("attempt,problem_id\n0,p1\n0,p1\n0,p1\n")
    aggregate_before = aggregate.read_text()

    result = stabilize_run_csv_headers(tmp_path)

    assert set(result.rewritten) == {narrow}  # only the genuine per-attempt file
    assert result.overflow_dropped == []
    assert rollup.read_text() == rollup_before
    assert aggregate.read_text() == aggregate_before


def test_a_ragged_rows_unlabeled_overflow_is_dropped_not_raised(tmp_path):
    """A row with more values than its own file's header lands DictReader's
    overflow under a None key (restkey). DictWriter has no fieldname to
    write that under and raises ValueError by default -- this must not
    propagate out of an optional, best-effort cleanup pass."""
    ragged = _publish(tmp_path, "agent", "p1", 0, "attempt,problem_id\n0,p1,unexpected,extra\n")
    _publish(tmp_path, "agent", "p2", 0, "Mitigation.success,attempt,problem_id\nTrue,0,p2\n")

    result = stabilize_run_csv_headers(tmp_path)  # must not raise

    assert set(result.rewritten) == {ragged}  # other already had the full header
    assert result.overflow_dropped == [ragged]  # the signal a caller should warn on
    assert _header(ragged) == ["Mitigation.success", "attempt", "problem_id"]
    assert _rows(ragged)[0]["problem_id"] == "p1"


def test_a_file_that_cannot_be_decoded_is_skipped_not_fatal(tmp_path):
    """A file with a byte that is not valid UTF-8 must not crash the whole
    normalization pass -- skip it, same as _rewrite_csv's own guard on the
    same failure mode, and still reconcile every file that can be read."""
    good_a = _publish(tmp_path, "agent", "p1", 0, "attempt,problem_id\n0,p1\n")
    _publish(tmp_path, "agent", "p2", 0, "Mitigation.success,attempt,problem_id\nTrue,0,p2\n")
    bad = _publish(tmp_path, "agent", "p3", 0, "attempt,problem_id\n0,p3\n")
    bad.write_bytes(b"attempt,problem_id\n0,\xffp3\n")
    bad_before = bad.read_bytes()

    result = stabilize_run_csv_headers(tmp_path)  # must not raise

    assert set(result.rewritten) == {good_a}  # good_b was already the full header
    assert result.overflow_dropped == []  # skipped, not the ragged-row case
    assert _header(good_a) == ["Mitigation.success", "attempt", "problem_id"]
    assert bad.read_bytes() == bad_before  # left exactly as it was, not rewritten


def test_a_ragged_file_is_rewritten_even_when_its_header_already_matches(tmp_path):
    """The overflow report must be true: a file flagged for a dropped
    overflow has to actually be rewritten without it."""
    ragged = _publish(tmp_path, "agent", "p1", 0, "attempt,problem_id\n0,p1,stray\n")
    _publish(tmp_path, "agent", "p2", 0, "attempt,problem_id\n0,p2\n")

    result = stabilize_run_csv_headers(tmp_path)

    assert result.overflow_dropped == [ragged]
    assert result.rewritten == [ragged]
    assert ragged.read_text() == "attempt,problem_id\n0,p1\n"


def test_agent_artifacts_inside_the_run_directory_are_left_alone(tmp_path):
    """The run directory also holds whatever the agent wrote. A CSV of the
    agent's own, even one that happens to match the name pattern, or a
    symlink the agent planted, must not join the union or be rewritten."""
    narrow = _publish(tmp_path, "agent", "p1", 0, "attempt,problem_id\n0,p1\n")
    _publish(tmp_path, "agent", "p2", 0, "Mitigation.success,attempt,problem_id\nTrue,0,p2\n")

    workspace = tmp_path / "agent" / "p1" / "run_0" / "workspace" / "run_7"
    workspace.mkdir(parents=True)
    agents_own = workspace / "p1_results.csv"
    agents_own.write_text("agent_column\nx\n")
    other_name = tmp_path / "agent" / "p2" / "run_0" / "notes_results.csv"
    other_name.write_text("stray\n1\n")
    link = tmp_path / "agent" / "p2" / "run_1"
    link.mkdir()
    (link / "p2_results.csv").symlink_to(agents_own)

    result = stabilize_run_csv_headers(tmp_path)

    assert set(result.rewritten) == {narrow}
    assert _header(narrow) == ["Mitigation.success", "attempt", "problem_id"]
    assert agents_own.read_text() == "agent_column\nx\n"
    assert other_name.read_text() == "stray\n1\n"
