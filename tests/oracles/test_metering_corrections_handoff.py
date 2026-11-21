from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from sregym.conductor.oracles.apps.metering.corrections_handoff import (
    MeteringOracle,
    differences,
    digest_rows,
    usage_charge,
)
from sregym.conductor.problems.apps.metering import reference_repair
from sregym.service.apps.metering.application import ledger_conf, publications_conf


def test_differences_treats_missing_as_zero():
    expected = {(1, "pell"): Decimal("2.5"), (2, "quill"): Decimal("1")}
    actual = {(1, "pell"): Decimal("2.5000"), (2, "quill"): Decimal("0"), (3, "vale"): Decimal("0")}
    assert differences(expected, actual) == [(2, "quill")]


def test_differences_reports_moves_on_both_accounts():
    expected = {(7, "garnet"): Decimal("4")}
    actual = {(7, "larkspur"): Decimal("4")}
    assert differences(expected, actual) == [(7, "garnet"), (7, "larkspur")]


def test_digest_rows_splits_on_first_bar():
    assert digest_rows("10|abc\n11|d|e\n") == {"10": "abc", "11": "d|e"}


@pytest.mark.parametrize(
    ("billing_rate", "finance_rate", "matches"),
    [
        ("(1.5,)", "(1.5)", True),
        ("(1.5,committed)", "(1.5)", False),
        ("(1.5,committed)", "(1.50,committed)", True),
        ("(1.5,)", '(1.5,"")', True),
        ("(1.5,committed)", "(1.6,committed)", False),
        ("(1.5,)", None, False),
        ("(1.5,)", "(invalid,)", False),
    ],
)
def test_check_rates_compares_price_and_band(billing_rate, finance_rate, matches):
    app = SimpleNamespace(
        billing=Mock(return_value=f"10|{billing_rate}\n"),
        finance=Mock(return_value=f"10|{finance_rate}\n" if finance_rate is not None else ""),
    )
    oracle = MeteringOracle(SimpleNamespace(app=app))

    assert oracle.check_rates() == ([] if matches else [oracle.fail("finance_rates_wrong", lines=[10], count=1)])


def test_finance_repair_preserves_equivalent_rate_snapshots(monkeypatch):
    app = SimpleNamespace(
        billing=Mock(side_effect=["1|(1.5,)\n2|(1.5,)\n3|(1.5,committed)\n", ""]),
        finance=Mock(return_value="1|(1.5)\n2|(1.5)\n3|(1.5)\n"),
        rows=Mock(return_value=[]),
    )
    problem = SimpleNamespace(app=app, load_state=lambda: {"handoff_at": "2026-11-20T00:00:00+00:00"})
    monkeypatch.setattr(reference_repair, "finance_differences", lambda app: [(2, "pell")])

    reference_repair.repair_finance(problem)

    assert "where line_id in (2,3)" in app.billing.call_args.args[0]
    assert "rate = case when r.line_id in (3) then r.rate else f.rate end" in app.finance.call_args.args[0]


@pytest.mark.parametrize(
    ("rate", "expected"),
    [
        ("(0.002000)", "1.0006"),
        ("(0.002000,)", "1.0006"),
        ("(0.002000,list)", "1.0006"),
        ("(0.002000,committed)", "0.7505"),
    ],
)
def test_only_committed_rates_get_the_contract_discount(rate, expected):
    assert usage_charge("quill", Decimal("1.0006"), rate) == Decimal(expected)
    assert usage_charge("pell", Decimal("1.0006"), rate) == Decimal("1.0006")


def test_failed_precheck_still_runs_jobs_and_reports_later_failures(monkeypatch):
    app = Mock()
    app.finance.return_value = "12"
    app.customer.return_value = "larkspur_monthend"
    app.close_statement.return_value = 3
    app.load_extract_files.side_effect = RuntimeError("loader did not finish")
    oracle = MeteringOracle(SimpleNamespace(app=app, load_state=dict))
    monkeypatch.setattr(oracle, "observe", lambda: {})
    monkeypatch.setattr(oracle, "subscription_errors", dict)
    monkeypatch.setattr(oracle, "lagging_feeds", lambda databases: ["larkspur_monthend"])
    monkeypatch.setattr(oracle, "check_documents", list)
    monkeypatch.setattr(oracle, "onboard_new_customer", lambda: None)
    monkeypatch.setattr(oracle, "fresh_corrections", lambda: [22])
    monkeypatch.setattr(oracle, "nightly_files", lambda: None)
    monkeypatch.setattr(oracle, "wait_ledgers_delivered", list)
    monkeypatch.setattr(oracle, "check_readers", lambda errors: [oracle.fail("customer_copy_wrong")])
    monkeypatch.setattr(oracle, "check_statements", lambda closes, touched: [])
    monkeypatch.setattr(oracle, "check_ledgers", lambda boundary: [oracle.fail("charges_wrong")])
    monkeypatch.setattr(oracle, "check_rates", list)
    monkeypatch.setattr(oracle, "check_statement_return", lambda: [oracle.fail("statement_return_waiver_wrong")])
    monkeypatch.setattr(oracle, "check_ingest", lambda: [oracle.fail("ingest_wrong")])

    result = oracle.evaluate()

    assert not result["success"]
    assert [failure["reason"] for failure in result["detail"]["failures"]] == [
        "subscription_not_streaming",
        "routine_jobs_failed",
        "customer_copy_wrong",
        "charges_wrong",
        "statement_return_waiver_wrong",
        "ingest_wrong",
    ]
    app.invoice_run.assert_called_once()
    app.set_rate.assert_called_with(60)


def test_publications_conf_declares_both_feeds_per_tenant():
    conf = publications_conf("r3", "x", {"tenants": ["pell"], "pell": ("insert", "update,delete")})
    assert conf.splitlines()[0] == "# revision: r3 x"
    assert "usage_pell | usage_events | tenant_id = 'pell' | insert" in conf
    assert "corr_pell | usage_events | tenant_id = 'pell' | update,delete" in conf
    assert "billing_usage | usage_events | | insert,update,delete" in conf


def test_ledger_conf():
    conf = ledger_conf("r2", "y", "insert,update,delete", "update,delete")
    assert "ledger_corr | ledger_lines | | update,delete" in conf


def test_daily_schedule_is_half_a_day_ahead_and_wraps_midnight():
    from datetime import UTC, datetime

    from sregym.service.apps.metering.application import daily_schedule

    assert daily_schedule(datetime(2026, 11, 20, 18, 50, tzinfo=UTC), 12 * 60 + 20) == "10 7 * * *"
    assert daily_schedule(datetime(2000, 1, 1, 0, 5, tzinfo=UTC), -15) == "50 23 * * *"


def test_extracts_conf_round_trip():
    from sregym.service.apps.metering.application import extracts_conf, parse_extracts_conf

    conf = extracts_conf({"pell": "SUP-4101", "larkspur": "SUP-4202"})
    assert conf.splitlines()[0].startswith("#")
    assert parse_extracts_conf(conf) == {"pell": "SUP-4101", "larkspur": "SUP-4202"}
    assert parse_extracts_conf("# account | ticket\n  larkspur | SUP-1 \n\n") == {"larkspur": "SUP-1"}


def test_rule_reasons_are_finance_rules_own():
    from sregym.conductor.oracles.apps.metering.corrections_handoff import is_rule_reason

    assert is_rule_reason("usage voided after invoice")
    assert is_rule_reason("usage moved to garnet")
    assert is_rule_reason("usage moved from pell")
    assert is_rule_reason("usage re-rated after invoice")
    assert not is_rule_reason("refund 3 already returned this usage")
    assert not is_rule_reason("credit note 1 already refunded this usage")


def test_notice_noise_flags_cancelling_or_mixed_rule_rows_only():
    from sregym.conductor.oracles.apps.metering.corrections_handoff import notice_noise

    booked = {
        (1, "pell"): [Decimal("-2")],
        (2, "pell"): [Decimal("-2"), Decimal("2")],
        (3, "quill"): [Decimal("-1"), Decimal("0.5")],
        (4, "garnet"): [Decimal("3"), Decimal("1")],
    }
    assert notice_noise(booked) == [(2, "pell"), (3, "quill")]


def test_charges_off_allows_a_cent():
    from sregym.conductor.oracles.apps.metering.corrections_handoff import charges_off

    owed = {"pell": Decimal("10.0000"), "vale": Decimal("5")}
    charged = {"pell": Decimal("10.01"), "vale": Decimal("4.98"), "wren": Decimal("0")}
    assert charges_off(owed, charged, {"pell", "vale", "wren"}) == {"vale": "-0.02"}


def test_changed_documents_reports_edits_and_deletions_not_new_rows():
    from sregym.conductor.oracles.apps.metering.corrections_handoff import changed_documents

    before = {"invoice:1": "a", "invoice:2": "b", "refund:1": "c"}
    after = {"invoice:1": "a", "invoice:2": "x", "invoice:3": "new"}
    assert changed_documents(before, after) == ["invoice:2", "refund:1"]


@pytest.mark.parametrize(
    ("quantities", "reason"),
    [([], "duplicate_usage_missing"), ([4], None), ([4, 4], "usage_entered_twice"), ([2], "duplicate_usage_changed")],
)
def test_duplicate_check_preserves_one_unchanged_usage_event(quantities, reason):
    usage = {"tenant_id": "larkspur", "metric": "calls", "quantity": 4, "occurred_at": "2026-11-20T00:00:00+00:00"}
    app = SimpleNamespace(core=Mock(), rows=Mock(return_value=[{**usage, "quantity": q} for q in quantities]))
    oracle = MeteringOracle(SimpleNamespace(app=app, load_state=lambda: {"w": 7, "w2": 11, "duplicate_usage": usage}))

    failures = oracle.check_duplicate_usage()

    assert [failure["reason"] for failure in failures] == ([reason] if reason else [])


def test_forwarded_from_reads_the_position_core_logged_for_that_slot():
    from sregym.conductor.problems.apps.metering.reference_repair import forwarded_from

    log = (
        "2026-11-20 10:32:03.846 UTC [69] LOG:  0/15A0000 has been already streamed, forwarding to 0/1DAA5C0\n"
        '2026-11-20 10:32:03.846 UTC [69] LOG:  starting logical decoding for slot "vale_usage"\n'
        "2026-11-20 10:32:09.100 UTC [71] LOG:  0/15A32E8 has been already streamed, forwarding to 0/1DAA5C0\n"
        '2026-11-20 10:32:09.100 UTC [71] STATEMENT:  START_REPLICATION SLOT "larkspur_monthend" LOGICAL 0/15A32E8\n'
    )
    app = SimpleNamespace(command=Mock(return_value=log))
    assert forwarded_from(app, "larkspur_monthend", "0/1DAA5C0") == "0/15A32E8"


def test_files_are_recalled_through_their_publication_and_both_settings_restored():
    from sregym.conductor.problems.apps.metering.reference_repair import recall_files

    app = SimpleNamespace(core=Mock())
    recall_files(app, "pell")
    script = app.core.call_args.args[0]
    steps = [line for line in script.splitlines() if line]
    assert steps[0] == "begin;" and steps[-1] == "commit;"
    assert steps.index("alter table extract_files replica identity full;") < steps.index(
        "update extract_files set status = 'withdrawn' where account = 'pell' and status = 'ready';"
    )
    assert "alter publication files_pell set (publish = 'insert, update');" in steps
    assert steps[-3:-1] == [
        "alter publication files_pell set (publish = 'insert');",
        "alter table extract_files replica identity default;",
    ]
    recall_files(app, "pell", through_feed=False)
    assert "publication" not in app.core.call_args.args[0]


def test_a_voided_row_is_deleted_from_its_correction_image_whether_the_reader_holds_it_or_not():
    from sregym.conductor.problems.apps.metering.reference_repair import net_transaction

    calls = []

    def core(script):
        calls.append(script)
        if script.startswith("select before from usage_corrections"):
            return '{"event_id": 7, "tenant_id": "larkspur"}\n'
        if script.startswith("select event_id || '|' || tenant_id"):
            return ""  # the voided row is no longer on core
        return "0/1\n"

    app = SimpleNamespace(core=core)
    net_transaction(
        app,
        {"corr_larkspur": "update, delete"},
        "larkspur",
        "larkspur_monthend",
        {"missing": [], "extra": [], "changed": []},
        skip={7},
        deletes=[7],
    )
    emitted = next(c for c in calls if c.startswith("begin;"))
    assert "jsonb_populate_record(null::usage_events" in emitted
    assert emitted.index("insert into usage_events") < emitted.index(
        "alter publication corr_larkspur set (publish = 'update, delete');"
    )
    assert emitted.index("alter publication corr_larkspur set (publish = 'update, delete');") < emitted.index(
        "delete from usage_events where event_id = 7;"
    )


@pytest.mark.parametrize(
    ("genuine", "missing", "retry", "history", "reasons"),
    [
        ((True, False), (False, True), "t", "line", []),
        ((False, True), (False, True), "t", "line", ["statement_return_waiver_wrong"]),
        ((True, False), (True, False), "t", "line", ["statement_return_waiver_wrong"]),
        ((True, False), (False, True), "f", "line", ["statement_return_not_imported"]),
        ((True, False), (False, True), "t", "line\nreversal\nline", ["statement_lines_wrong"]),
    ],
)
def test_statement_return_requires_the_retry_and_both_charge_decisions(genuine, missing, retry, history, reasons):
    app = SimpleNamespace(
        core=Mock(return_value=retry),
        finance=Mock(),
        customer=Mock(return_value=history),
        rows=Mock(
            return_value=[
                {"event": event, "waived": waived, "charged": charged}
                for event, (waived, charged) in ((1, genuine), (2, missing))
            ]
        ),
    )
    state = {"statement_return": {"genuine": 1, "missing": 2, "retry": 9}}
    oracle = MeteringOracle(SimpleNamespace(app=app, load_state=lambda: state))

    assert [failure["reason"] for failure in oracle.check_statement_return()] == reasons


# ------------------------------------------------------------ v6 complaints
from sregym.conductor.problems.apps.metering import complaints  # noqa: E402

SUBSCRIBER_LOG = """2026-11-21 01:00:00.100 UTC [41] LOG:  logical replication apply worker for subscription "ostrava_usage" has started
2026-11-21 01:00:00.200 UTC [41] ERROR:  duplicate key value violates unique constraint "usage_events_pkey"
2026-11-21 01:00:00.200 UTC [41] DETAIL:  Key (event_id)=(812) already exists.
2026-11-21 01:00:00.200 UTC [41] CONTEXT:  processing remote data for replication origin "pg_16501" during message type "INSERT" for replication target relation "public.usage_events" in transaction 9001, finished at 0/D1A2B30
2026-11-21 01:00:00.300 UTC [42] ERROR:  duplicate key value violates unique constraint "usage_events_pkey"
2026-11-21 01:00:00.300 UTC [42] CONTEXT:  processing remote data for replication origin "pg_16999" during message type "INSERT" for replication target relation "public.usage_events" in transaction 9002, finished at 0/D1B0000
"""


def test_failed_transaction_reads_one_origin_with_its_pasted_lines():
    found = complaints.failed_transaction(SUBSCRIBER_LOG, "pg_16501")
    assert found["xid"] == 9001 and found["finish"] == "0/D1A2B30"
    assert found["pasted"].splitlines()[0].endswith('unique constraint "usage_events_pkey"')
    assert "Key (event_id)=(812)" in found["pasted"] and "[42]" not in found["pasted"]
    assert complaints.failed_transaction(SUBSCRIBER_LOG, "pg_1") is None


def test_lsn_value_compares_numerically():
    assert complaints.lsn_value("1/A") == (1 << 32) + 10
    assert complaints.lsn_value("0/FFFF") < complaints.lsn_value("0/D1A0000")


CORE_LOG = """2026-11-21 00:59:00.000 UTC [10] LOG:  starting logical decoding for slot "quill_usage"
2026-11-21 01:00:01.000 UTC [20] LOG:  0/100 has been already streamed, forwarding to 0/180
2026-11-21 01:00:01.000 UTC [20] STATEMENT:  START_REPLICATION SLOT "quill_usage" LOGICAL 0/100 (proto_version '4', origin 'any', publication_names '"usage_quill","corr_quill","open_quill"')
2026-11-21 01:00:01.000 UTC [20] DETAIL:  Streaming transactions committing after 0/180, reading WAL from 0/80.
2026-11-21 01:00:02.000 UTC [30] LOG:  0/100 has been already streamed, forwarding to 0/280
2026-11-21 01:00:02.000 UTC [30] STATEMENT:  START_REPLICATION SLOT "larkspur_live" LOGICAL 0/100 (proto_version '4', origin 'any', publication_names '"usage_larkspur","corr_larkspur"')
2026-11-21 01:00:02.000 UTC [30] DETAIL:  Streaming transactions committing after 0/280, reading WAL from 0/80.
2026-11-21 01:00:03.000 UTC [40] LOG:  checkpoint complete: lsn=0/300, redo lsn=0/2F0
"""
# Quill's failing batch is xid 7 and Larkspur's xid 8; each reconnect forwards to a record end inside its own.
WAL = "\n".join([
    "0/F0|0/100|Transaction|COMMIT|5", "0/100|0/110|Transaction|COMMIT|6", "0/80|0/98|Standby|RUNNING_XACTS|0",
    "0/170|0/180|Heap|LOCK|7", "0/180|0/1A0|Heap|INSERT|7", "0/1A0|0/1C0|Transaction|COMMIT|7",
    "0/270|0/280|Heap|LOCK|8", "0/280|0/2A0|Heap|INSERT|8", "0/2A0|0/2C0|Transaction|COMMIT|8",
    "0/400|0/420|Transaction|COMMIT|9",
])


def evidence_app(log=CORE_LOG, level="warning|default", sampled=None):
    sampled = sampled or [{"slot": "quill_usage", "restart": "0/80", "confirmed": "0/420"},
                          {"slot": "larkspur_live", "restart": "0/80", "confirmed": "0/500"}]
    core = Mock(side_effect=lambda q, **_: level if "log_min_messages" in q else WAL)
    return SimpleNamespace(core=core, command=Mock(return_value=log), rows=Mock(return_value=[dict(r) for r in sampled]))


BATCHES = {
    "quill_usage": {"remedy": "skip", "since": "2026-11-21 01:00:00.000", "xid": 7, "finish": "0/1A0"},
    "larkspur_live": {"remedy": "local_delete", "since": "2026-11-21 01:00:00.000", "xid": 8, "finish": "0/2A0"},
}


def evidence(app):
    return complaints.ComplaintReplay(SimpleNamespace(app=app)).publisher_evidence(BATCHES)


def test_reader_lines_groups_connections_after_the_batch_by_slot():
    lines = complaints.reader_lines(CORE_LOG, "2026-11-21 01:00:00.000")
    assert sorted(lines) == ["larkspur_live", "quill_usage"]
    assert [len(c) for c in lines["quill_usage"]] == [3]


def test_reconnect_positions_in_each_readers_own_batch_leave_the_same_evidence():
    assert evidence(evidence_app()) == []


def test_a_line_only_the_skipped_reader_shows_fails_the_comparison():
    extra = CORE_LOG + ("2026-11-21 01:00:01.000 UTC [20] DEBUG:  failed to increase restart lsn: "
                        "proposed 0/80, after 0/80, current candidate 0/80, current after 0/80, flushed up to 0/180\n")
    assert any("never shows" in p for p in evidence(evidence_app(log=extra)))


def test_a_debug_log_level_and_a_mid_transaction_sample_fail():
    sampled = [{"slot": "quill_usage", "restart": "0/80", "confirmed": "0/180"},
               {"slot": "larkspur_live", "restart": "0/80", "confirmed": "0/500"}]
    problems = evidence(evidence_app(level="debug1|configuration file", sampled=sampled))
    assert any("log_min_messages" in p for p in problems)
    assert any("quill_usage sampled position 0/180" in p for p in problems)


def test_a_logged_position_inside_another_transaction_fails():
    moved = CORE_LOG.replace("forwarding to 0/180", "forwarding to 0/280")
    assert any("quill_usage progress position 0/280 is on other_txn" in p
               for p in evidence(evidence_app(log=moved)))


def oracle_with(truth, rows):
    oracle = MeteringOracle.__new__(MeteringOracle)
    app = SimpleNamespace(core=Mock(return_value=""), rows=Mock(return_value=rows))
    oracle.problem = SimpleNamespace(app=app, load_state=lambda: {"complaints": {"truth": truth}})
    oracle.fail = lambda reason, **detail: {"reason": reason, "detail": detail}
    return oracle


def test_core_usage_wrong_flags_a_restored_row_and_a_voided_row():
    truth = {
        "5": {"tenant_id": "garnet", "metric": "api_calls", "quantity": 3.0, "occurred_at": "t1"},
        "6": {"tenant_id": "quill", "metric": "tokens_k", "quantity": 1.5, "occurred_at": "t2"},
        "7": {"tenant_id": "vale", "metric": "api_calls", "quantity": 2.0, "occurred_at": "t3"},
    }
    rows = [
        {"event_id": 5, "tenant_id": "larkspur", "metric": "api_calls", "quantity": 3.0, "occurred_at": "t1"},
        {"event_id": 6, "tenant_id": "quill", "metric": "tokens_k", "quantity": 1.5, "occurred_at": "t2"},
    ]
    assert oracle_with(truth, rows).check_core_usage() == [
        {"reason": "core_usage_wrong", "detail": {"events": [5, 7], "count": 2}}
    ]
    assert oracle_with(truth, [{"event_id": int(e), **r} for e, r in truth.items()]).check_core_usage() == []


def test_assume_applied_leaves_out_only_a_skipped_batch():
    state = {"complaints": {"batches": {
        "quill_usage": {"remedy": "skip", "rights": [1, 2], "redate": 3},
        "larkspur_live": {"remedy": "local_delete", "rights": [4]},
    }}}
    assert reference_repair.skipped_items(state, "quill_usage", ("assume_applied",)) == {1, 2, 3}
    assert reference_repair.skipped_items(state, "quill_usage", ()) == set()
    assert reference_repair.skipped_items(state, "larkspur_live", ("assume_applied",)) == set()


# ------------------------------------------------------------ v6 ordinary closes and Pell classes
from sregym.conductor.problems.apps.metering import allocation_close  # noqa: E402


def test_ordinary_closes_stagger_filings_and_leave_nineteen_of_twenty_eight_returns_off_eligible():
    plans = [allocation_close.noise_plan(i) for i in range(24)]
    assert all(set(plan) == {3} for plan in plans[::6])
    pending = [(rev, 3) for i in allocation_close.PENDING for rev in plans[i]]
    pending += [(rev, 6) for rev in allocation_close.FILED_REVISIONS]
    assert len(pending) == 28 and sum(rev != eligible for rev, eligible in pending) == 19


def returns_oracle(deliveries, waived=()):
    oracle = MeteringOracle.__new__(MeteringOracle)
    state = {"allocation_returns": {
        "close": "AR-25", "earned": [{"ref": "AR-25-pell", "line": 1, "account": "pell", "amount": 5}],
        "genuine": [{"ref": "AR-20-pell", "line": 2, "account": "pell"}],
        "pending": [{"ref": "AR-20-pell", "close": "AR-20", "account": "pell", "revision": 2}],
    }}
    decisions = [{"line": 1, "account": "pell", "waiver": None, "charges": 5, "items": 1, "billed_account": "pell"},
                 {"line": 2, "account": "pell", "waiver": "AR-20-pell", "charges": 0, "items": 0, "billed_account": None}]
    finance = Mock(return_value="")
    app = SimpleNamespace(core=Mock(return_value=""), finance=finance)
    app.rows = lambda run, q: deliveries if run is not finance else (
        list(waived) if "waived_by_return in" in q else decisions)
    oracle.problem = SimpleNamespace(app=app, load_state=lambda: state)
    oracle.state = lambda: state
    oracle.fail = lambda reason, **detail: {"reason": reason, "detail": detail}
    return oracle


def test_every_pending_close_must_be_imported_with_its_waivers_unchanged():
    done = [{"ref": "AR-25-pell", "retried": True, "pending": False, "acknowledged": False},
            {"ref": "AR-20-pell", "retried": True, "pending": False, "acknowledged": False}]
    assert returns_oracle(done).check_allocation_returns(10) == []
    held = [done[0], {**done[1], "pending": True}]
    assert [f["detail"] for f in returns_oracle(held).check_allocation_returns(10)] == [{"returns": ["AR-20-pell"]}]
    extra = [{"line": 3, "waiver": "AR-20-pell"}, {"line": 2, "waiver": "AR-20-pell"}]
    assert [f["detail"]["event"] for f in returns_oracle(done, extra).check_allocation_returns(10)] == [3]


def test_revision_filter_holds_off_eligible_returns_in_the_import_query():
    script = "\\copy (select delivery_id from statement_return_inbox where imported_at is null order by delivery_id) to stdout"
    data = {"statement-return-import.sh": script, "invoice-run.sh": "x"}
    applied = []
    app = SimpleNamespace(command=Mock(return_value=__import__("json").dumps({"data": data})),
                          config_map=lambda name, files: (name, files), apply_documents=applied.extend)
    reference_repair.import_eligible_only(app)
    name, files = applied[0]
    assert name == "finance-jobs" and files["invoice-run.sh"] == "x"
    assert "batch_revision = (select eligible_revision" in files["statement-return-import.sh"]


def test_pell_residuals_each_fall_in_exactly_one_class():
    from sregym.conductor.problems.apps.metering.corrections_handoff import MeteringCorrectionsHandoff

    files = [[{"event_id": 30}, {"event_id": 31}]]
    problem = MeteringCorrectionsHandoff.__new__(MeteringCorrectionsHandoff)
    problem.app = SimpleNamespace(customer=Mock(return_value=""), rows=lambda run, q: files)
    facts = {"pell_gap": [100, 200], "pell_gap_from": 90, "pell_gap_corrected": [40],
             "statement_return": {"genuine": 50, "missing": 51}}
    pell = {"extra": [30], "missing": [95, 150, 51], "changed": [40, 50]}
    assert problem.seal_pell_classes(facts, pell) == []
    assert facts["pell_classes"]["advance_granularity"] == [40, 95, 150]
    pell["changed"].append(60)
    assert problem.seal_pell_classes(facts, pell) == ["Pell residual 60 is in no class"]
    facts["pell_gap_corrected"].append(30)
    pell["changed"].append(30)
    assert "Pell residual 30 is in ['advance_granularity', 'loader_rebook']" in problem.seal_pell_classes(facts, pell)
