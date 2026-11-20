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
    monkeypatch.setattr(oracle, "check_ingest", lambda: [oracle.fail("ingest_wrong")])

    result = oracle.evaluate()

    assert not result["success"]
    assert [failure["reason"] for failure in result["detail"]["failures"]] == [
        "subscription_not_streaming",
        "routine_jobs_failed",
        "customer_copy_wrong",
        "charges_wrong",
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
