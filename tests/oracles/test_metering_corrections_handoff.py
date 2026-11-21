from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest

from sregym.conductor.oracles.apps.metering.corrections_handoff import (
    MeteringOracle,
    differences,
    digest_rows,
    usage_charge,
)
from sregym.conductor.problems.apps.metering import reference_repair
from sregym.conductor.problems.apps.metering.corrections_handoff import MeteringCorrectionsHandoff
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
    oracle.expected_rates = lambda: {10: billing_rate}

    assert oracle.check_rates() == ([] if matches else [oracle.fail("finance_rates_wrong", lines=[10], count=1)])


def test_matching_ledgers_with_the_wrong_historical_band_fail():
    app = SimpleNamespace(billing=Mock(return_value="10|(1.5)"), finance=Mock(return_value="10|(1.5)"))
    oracle = MeteringOracle(SimpleNamespace(app=app))
    oracle.expected_rates = lambda: {10: "(1.5,committed)"}
    assert oracle.check_rates() == [oracle.fail("finance_rates_wrong", lines=[10], count=1)]


@pytest.mark.parametrize(
    ("rate", "charged", "rejected"),
    [("(0.002,committed)", "7.5000", True), ("(0.002,)", "10.0000", False)],
    ids=["repriced", "original-rate"],
)
def test_rebuilt_legacy_line_keeps_its_original_rate(rate, charged, rejected):
    released_at = "2026-11-20 00:00:00+00"
    rebuilt_rated_at = "2026-11-21 00:00:00+00"

    def core(query, database="usage"):
        if database == "payments":
            return ""
        if "round(quantity" in query:
            return "10|quill|10.0000"
        assert "from usage_events" in query
        return "10|api_calls"

    def billing(query):
        if "rated_at" in query:
            assert released_at in query
            band = "committed" if rebuilt_rated_at >= released_at else ""
            return f"10|(0.002,{band})"
        if "rate::text" in query:
            return f"10|{rate}"
        assert "account || '|' || amount" in query
        return "10|quill|10.0000"

    def finance(query):
        if "rate::text" in query:
            return f"10|{rate}"
        if "from invoice_items" in query:
            return f"10|quill|usage|{charged}|2"
        if "from invoices" in query:
            return f"2|quill|{charged}|{charged}"
        if "from credit_notes" in query:
            return ""
        assert "count(*) from ledger_lines" in query
        return "0"

    state = {
        "rate_band_release": released_at,
        "rate_cohorts": {"legacy": [[10, 10]], "release": [], "patched": []},
        "statement_return": {"genuine": 99},
    }
    app = SimpleNamespace(core=core, billing=billing, finance=finance)
    oracle = MeteringOracle(SimpleNamespace(app=app, load_state=lambda: state))
    rate_failures = [oracle.fail("finance_rates_wrong", lines=[10], count=1)] if rejected else []
    ledger_failures = []
    if rejected:
        ledger_failures = [
            oracle.fail("invoice_rates_wrong", lines=[10], count=1),
            oracle.fail("finance_ledger_wrong", lines=[[10, "quill"]], count=1),
            oracle.fail("charges_wrong", accounts={"quill": "-2.50"}, invoices=[], captures=[]),
        ]

    assert (oracle.check_rates(), oracle.check_ledgers(invoice_boundary=1)) == (rate_failures, ledger_failures)


def test_sealed_rate_cohorts_keep_gaps_and_exclude_voided_usage():
    app = SimpleNamespace(
        billing=Mock(
            return_value=(
                "10|(0.002,)|legacy\n11|(0.002,)|legacy\n13|(0.002,)|legacy\n"
                "14|(0.002,committed)|release\n15|(0.002,committed)|release\n16|(0.0400,list)|patched"
            )
        ),
        finance=Mock(
            side_effect=[
                "10|(0.002)\n11|(0.002)\n13|(0.002)\n14|(0.002)\n15|(0.002)\n16|(0.0400,list)",
                "10|quill\n11|hollis\n14|quill\n15|hollis",
            ]
        ),
        core=Mock(return_value="11|api_calls\n12|api_calls\n13|api_calls\n14|api_calls\n15|api_calls\n16|storage_gb_h"),
    )
    problems, _, cohorts = MeteringCorrectionsHandoff.seal_rates(
        SimpleNamespace(app=app), patched_at="2026-11-20 00:00:00+00"
    )
    assert problems == []
    assert cohorts == {"legacy": [[10, 11], [13, 13]], "release": [[14, 15]], "patched": [[16, 16]]}

    oracle = MeteringOracle(SimpleNamespace(app=app, load_state=lambda: {"rate_cohorts": cohorts}))
    assert oracle.expected_rates() == {
        11: "(0.002,)",
        12: "(0.002,committed)",
        13: "(0.002,)",
        14: "(0.002,committed)",
        15: "(0.002,committed)",
        16: "(0.0400,list)",
    }


@pytest.mark.parametrize(
    ("wrong", "updated_lines", "rate_expression"),
    [
        ((), "2,3", "case when r.line_id in (3) then r.rate else f.rate end"),
        (("keep_rates",), "2", "f.rate"),
        (("fill_legacy_rates",), "1,2,3", "case when split_part(r.rate, ',', 2) = ')'"),
    ],
)
def test_finance_repair_rate_updates(monkeypatch, wrong, updated_lines, rate_expression):
    app = SimpleNamespace(
        billing=Mock(side_effect=["1|(1.5,)\n2|(1.5,)\n3|(1.5,committed)\n", ""]),
        finance=Mock(return_value="1|(1.5)\n2|(1.5)\n3|(1.5)\n"),
        rows=Mock(return_value=[]),
    )
    problem = SimpleNamespace(app=app, load_state=lambda: {"handoff_at": "2026-11-20T00:00:00+00:00"})
    monkeypatch.setattr(reference_repair, "finance_differences", lambda app: [(2, "pell")])

    reference_repair.repair_finance(problem, wrong=wrong)

    assert f"where line_id in ({updated_lines})" in app.billing.call_args.args[0]
    assert f"rate = {rate_expression}" in app.finance.call_args.args[0]


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


def test_grader_operations_are_classified_separately_and_jobs_still_run(monkeypatch):
    app = Mock()
    app.finance.return_value = "12"
    app.core.return_value = "42"
    app.customer.return_value = ""
    app.close_statement.return_value = 3
    app.load_extract_files.side_effect = RuntimeError("customer loader call failed")
    oracle = MeteringOracle(SimpleNamespace(app=app, load_state=dict))
    monkeypatch.setattr(oracle, "observe", dict)
    monkeypatch.setattr(oracle, "subscription_errors", lambda: {"quill_usage": 11})
    monkeypatch.setattr(oracle, "check_documents", list)
    monkeypatch.setattr(oracle, "onboard_new_customer", lambda: None)
    monkeypatch.setattr(oracle, "fresh_corrections", lambda: [22])
    monkeypatch.setattr(oracle, "nightly_files", lambda: None)
    monkeypatch.setattr(oracle, "wait_ledgers_delivered", list)
    monkeypatch.setattr(oracle, "check_readers", lambda excluded: [])
    monkeypatch.setattr(oracle, "check_statements", lambda db, close: [])
    monkeypatch.setattr(oracle, "check_ledgers", lambda boundary: [])
    monkeypatch.setattr(oracle, "check_rates", list)
    monkeypatch.setattr(oracle, "check_return_reconciliations", lambda cutoff: [])
    monkeypatch.setattr(oracle, "check_allocation_returns", list)
    monkeypatch.setattr(oracle, "check_ingest", list)

    result = oracle.evaluate()

    assert not result["success"]
    assert result["detail"]["failures"] == [
        {"reason": "oracle_raised", "error": "RuntimeError: customer loader call failed", "step": "Pell file loader"}
    ]
    app.invoice_run.assert_called_once()
    app.set_rate.assert_called_with(60)
    assert sum(call.args[0] == "statement-return-resend" for call in app.run_cronjob.call_args_list) == 2


def test_readers_grade_persistent_progress_without_counting_recovered_errors(monkeypatch):
    from sregym.conductor.oracles.apps.metering import corrections_handoff

    app = SimpleNamespace(customer=Mock(return_value=""))
    oracle = MeteringOracle(SimpleNamespace(app=app))
    monkeypatch.setattr(corrections_handoff, "copy_digest", lambda *args: {})
    monkeypatch.setattr(oracle, "subscription_errors", Mock(side_effect=AssertionError("counter is observational")))
    monkeypatch.setattr(oracle, "lagging_feeds", lambda databases: [])
    assert oracle.check_readers() == []
    monkeypatch.setattr(oracle, "lagging_feeds", lambda databases: ["larkspur_live"])
    assert oracle.check_readers() == [oracle.fail("subscription_not_streaming", feeds=["larkspur_live"])]


def test_database_readiness_retries_a_timed_out_probe(monkeypatch):
    import subprocess

    from sregym.service.apps.metering import application

    app = SimpleNamespace(command=Mock(), psql=Mock(side_effect=[subprocess.TimeoutExpired("probe", 30), "1"]))
    monkeypatch.setattr(application.time, "sleep", lambda _: None)
    application.MeteringApplication.wait_pod_ready(app, "core-0")
    assert app.psql.call_count == 2


def test_catalog_change_writer_can_outlive_both_setup_waits(monkeypatch):
    from sregym.service.apps.metering import application

    change = Mock()
    run = Mock(return_value="t")
    executor = MagicMock()
    pool = executor.return_value.__enter__.return_value
    writer = pool.submit.return_value
    writer.done.return_value = False
    writer.result.return_value = "0/5"
    monkeypatch.setattr(application, "ThreadPoolExecutor", executor)

    assert (
        application.MeteringApplication.change_while_writing(
            SimpleNamespace(), run, "writer", "select 1;", "select 1", change
        )
        == "0/5"
    )
    assert pool.submit.call_args.kwargs["timeout"] > 2 * application.CATALOG_CHANGE_TIMEOUT
    change.assert_called_once()


def test_nightly_extracts_runs_with_only_larkspur():
    app = Mock()
    app.command.side_effect = ['{"spec": {}}', '{"data":{"extracts.conf":"larkspur | SUP-123"}}']
    oracle = MeteringOracle(SimpleNamespace(app=app))
    oracle.nightly_files()
    assert app.run_cronjob.call_args.args[0] == "correction-extracts"


def test_pell_loader_waits_for_withdrawal_status():
    from sregym.service.apps.metering.application import ExtractDeliveryTimeout, MeteringApplication

    app = SimpleNamespace(core=Mock(return_value="81|withdrawn"), customer=Mock(return_value="81|ready"))
    with pytest.raises(ExtractDeliveryTimeout, match="81\\|withdrawn"):
        MeteringApplication.wait_extract_files(app, "pell", "pell_usage", timeout=-1)
    assert all("load_ready_files" not in call.args[1] for call in app.customer.call_args_list)


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


def test_finance_effective_counts_all_line_linked_adjustments():
    from sregym.conductor.oracles.apps.metering.corrections_handoff import finance_effective

    app = SimpleNamespace(finance=Mock(return_value="1|pell|10.0000\n1|pell|-7.5000"))
    assert finance_effective(app) == {(1, "pell"): Decimal("2.5000")}
    sql = app.finance.call_args.args[0]
    assert "where line_id is not null" in sql and "reason" not in sql


def test_notice_noise_allows_offsetting_adjustments_with_a_real_net_change():
    from sregym.conductor.oracles.apps.metering.corrections_handoff import notice_noise

    booked = {
        (1, "pell"): [Decimal("-2")],
        (2, "pell"): [Decimal("-2"), Decimal("2")],
        (3, "quill"): [Decimal("-10"), Decimal("3")],
        (4, "garnet"): [Decimal("3"), Decimal("1")],
    }
    assert notice_noise(booked) == [(2, "pell")]


def test_charges_compare_at_invoice_precision():
    from sregym.conductor.oracles.apps.metering.corrections_handoff import charges_off

    owed = {"pell": Decimal("10.004"), "vale": Decimal("5.005"), "quill": Decimal("-4.005")}
    charged = {"pell": Decimal("10.00"), "vale": Decimal("5.00"), "quill": Decimal("-4.01")}
    assert charges_off(owed, charged, owed) == {"vale": "-0.01"}


def test_charges_reconcile_each_invoice_at_its_stored_precision():
    app = SimpleNamespace(finance=Mock(), core=Mock())
    app.finance.side_effect = ["1|pell|0.01|0.0050\n2|pell|0.01|0.0050", ""]
    app.core.side_effect = ["", "1|1|pell|0.01"]
    oracle = MeteringOracle(SimpleNamespace(app=app))
    target = {(1, "pell"): Decimal("0.005"), (2, "pell"): Decimal("0.005")}
    assert oracle.check_charges(target) == []

    app.finance.side_effect = ["1|pell|0.00|0.0050\n2|pell|0.01|0.0050", ""]
    app.core.side_effect = ["", "1|1|pell|0.02"]
    assert oracle.check_charges(target) == [
        oracle.fail("charges_wrong", accounts={"pell": "-0.01"}, invoices=[1], captures=[1])
    ]


def test_active_statement_checks_values_and_preserves_only_correct_bookings():
    from sregym.conductor.oracles.apps.metering.corrections_handoff import statement_differences

    old = "2026-10-01|quill|api_calls|2.000"
    corrected = "2026-11-01|quill|api_calls|2.000"
    vale = "2026-11-01|vale|api_calls|3.000"
    truth = {1: corrected, 2: vale}
    kept = [(2, 9, vale)]
    assert statement_differences(truth, {1: [(8, corrected)], 2: [(9, vale)]}, kept) == ([], [])
    wrong = {1: [(7, old)], 2: [(10, vale)], 3: [(11, vale)]}
    assert statement_differences(truth, wrong, kept) == ([1, 3], [2])


def test_statement_reversal_protects_a_correct_line_and_allows_a_replacement_for_a_wrong_line():
    correct = "2026-11-01|quill|api_calls|2.000"
    app = SimpleNamespace(core=Mock(return_value=f"1|{correct}\n2|{correct}"), customer=Mock())
    app.customer.side_effect = [f"1|7|{correct}\n2|8|{correct}", f"2|9|{correct}|true"]
    oracle = MeteringOracle(SimpleNamespace(app=app))
    # Event 1's wrong-period booking never belonged to the sealed set.
    assert oracle.check_statements("quill_usage", 23) == [
        oracle.fail("statement_reversed", database="quill_usage", events=[2], count=1),
        oracle.fail("statement_rebooked", database="quill_usage", events=[2], count=1),
    ]


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
    ("genuine", "missing", "history", "reasons"),
    [
        (("PELL-1", False), (None, True), "line", []),
        (("OTHER", False), (None, True), "line", ["statement_return_waiver_wrong"]),
        ((None, True), (None, True), "line", ["statement_return_waiver_wrong"]),
        (("PELL-1", False), ("PELL-1", False), "line", ["statement_return_waiver_wrong"]),
        (("PELL-1", False), (None, True), "line\nreversal\nline", ["statement_lines_wrong"]),
    ],
)
def test_statement_return_checks_both_charge_decisions(genuine, missing, history, reasons):
    app = SimpleNamespace(
        finance=Mock(),
        customer=Mock(return_value=history),
        rows=Mock(
            return_value=[
                {"event": event, "waived": ref, "charged": charged}
                for event, (ref, charged) in ((1, genuine), (2, missing))
            ]
        ),
    )
    state = {"statement_return": {"genuine": 1, "missing": 2, "ref": "PELL-1"}}
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
WAL = "\n".join(
    [
        "0/F0|0/100|Transaction|COMMIT|5",
        "0/100|0/110|Transaction|COMMIT|6",
        "0/80|0/98|Standby|RUNNING_XACTS|0",
        "0/170|0/180|Heap|LOCK|7",
        "0/180|0/1A0|Heap|INSERT|7",
        "0/1A0|0/1C0|Transaction|COMMIT|7",
        "0/270|0/280|Heap|LOCK|8",
        "0/280|0/2A0|Heap|INSERT|8",
        "0/2A0|0/2C0|Transaction|COMMIT|8",
        "0/400|0/420|Transaction|COMMIT|9",
    ]
)


def evidence_app(log=CORE_LOG, level="warning|default", sampled=None):
    sampled = sampled or [
        {"slot": "quill_usage", "restart": "0/80", "confirmed": "0/420"},
        {"slot": "larkspur_live", "restart": "0/80", "confirmed": "0/500"},
    ]
    core = Mock(side_effect=lambda q, **_: level if "log_min_messages" in q else WAL)
    return SimpleNamespace(
        core=core, command=Mock(return_value=log), rows=Mock(return_value=[dict(r) for r in sampled])
    )


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


def test_a_debug_line_from_the_skipped_reader_fails():
    extra = CORE_LOG + (
        "2026-11-21 01:00:01.000 UTC [20] DEBUG:  failed to increase restart lsn: "
        "proposed 0/80, after 0/80, current candidate 0/80, current after 0/80, flushed up to 0/180\n"
    )
    assert any("quill_usage lines that name the remedy" in p for p in evidence(evidence_app(log=extra)))


def test_generic_restart_lines_on_one_reader_only_still_pass():
    extra = CORE_LOG + "2026-11-21 01:00:01.000 UTC [20] DETAIL:  Logical decoding will begin using saved snapshot.\n"
    assert evidence(evidence_app(log=extra)) == []


def test_a_reader_without_reconnect_lines_fails():
    quiet = "\n".join(line for line in CORE_LOG.splitlines() if "[30]" not in line) + "\n"
    assert any("no reconnect line for larkspur_live" in p for p in evidence(evidence_app(log=quiet)))


def test_a_debug_log_level_and_a_mid_transaction_sample_fail():
    sampled = [
        {"slot": "quill_usage", "restart": "0/80", "confirmed": "0/180"},
        {"slot": "larkspur_live", "restart": "0/80", "confirmed": "0/500"},
    ]
    problems = evidence(evidence_app(level="debug1|configuration file", sampled=sampled))
    assert any("log_min_messages" in p for p in problems)
    assert any("quill_usage sampled position 0/180" in p for p in problems)


def test_a_logged_position_inside_another_transaction_fails():
    moved = CORE_LOG.replace("forwarding to 0/180", "forwarding to 0/280")
    assert any("quill_usage progress position 0/280 is on other_txn" in p for p in evidence(evidence_app(log=moved)))


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
    state = {
        "complaints": {
            "batches": {
                "quill_usage": {"remedy": "skip", "rights": [1, 2], "redate": 3},
                "larkspur_live": {"remedy": "local_delete", "rights": [4]},
            }
        }
    }
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


def test_reconciliations_require_matching_signed_decisions_and_survive_resend():
    signed = [
        {"account": "pell", "ref": "AR-20", "digest": "pell-file"},
        {"account": "quill", "ref": "AR-20", "digest": "quill-file"},
    ]
    state = {"signed_returns": signed}
    records = [dict(row) for row in signed]
    app = SimpleNamespace(finance=Mock(), rows=Mock(return_value=records))
    oracle = MeteringOracle(SimpleNamespace(app=app, load_state=lambda: state))
    app.core = Mock(side_effect=["pell|AR-20\nquill|AR-20", ""])
    assert oracle.check_return_reconciliations(100) == []
    assert "or delivery_id > 100" in app.core.call_args.args[0]

    app.core = Mock(side_effect=["pell|AR-20\nquill|AR-20", "quill|AR-20"])
    records[0]["digest"] = "a-different-file"
    (failure,) = oracle.check_return_reconciliations(100)
    assert failure["detail"]["account_returns"] == [("pell", "AR-20"), ("quill", "AR-20")]


def test_allocation_checks_recorded_and_actual_waivers_on_all_returns():
    state = {
        "statement_return": {"ref": "PELL-1", "genuine": 9, "missing": 8},
        "allocation_returns": {
            "earned": [{"ref": "AR-25-pell", "line": 1, "account": "pell", "amount": 5}],
            "genuine": [{"ref": "AR-20-pell", "line": 2, "account": "pell"}],
        },
    }
    decisions = [
        {"line": 1, "account": "pell", "waiver": None, "charges": 5, "items": 1, "billed_account": "pell"},
        {"line": 2, "account": "pell", "waiver": "AR-20-pell", "charges": 0, "items": 0, "billed_account": None},
    ]
    good = [{"line": 2, "waiver": "AR-20-pell", "account": "pell"}, {"line": 9, "waiver": "PELL-1", "account": "pell"}]
    extra = {"line": 3, "waiver": "old-acknowledged", "account": "pell"}
    app = SimpleNamespace(finance=Mock(), rows=Mock(side_effect=[decisions, [*good, extra], good[1:]]))
    oracle = MeteringOracle(SimpleNamespace(app=app, load_state=lambda: state))
    assert {f["detail"]["event"] for f in oracle.check_allocation_returns()} == {2, 3}


def test_revision_filter_holds_off_eligible_returns_in_the_import_query():
    script = "\\copy (select delivery_id from statement_return_inbox where imported_at is null order by delivery_id) to stdout"
    data = {"statement-return-import.sh": script, "invoice-run.sh": "x"}
    applied = []
    app = SimpleNamespace(
        command=Mock(return_value=__import__("json").dumps({"data": data})),
        config_map=lambda name, files: (name, files),
        apply_documents=applied.extend,
    )
    reference_repair.import_eligible_only(app)
    name, files = applied[0]
    assert name == "finance-jobs" and files["invoice-run.sh"] == "x"
    assert "batch_revision = (select eligible_revision" in files["statement-return-import.sh"]


def test_pell_residuals_beyond_the_stale_window_each_have_a_class():
    from sregym.conductor.problems.apps.metering.corrections_handoff import MeteringCorrectionsHandoff

    files = [[{"event_id": 30}, {"event_id": 31}, {"event_id": 40}]]
    problem = MeteringCorrectionsHandoff.__new__(MeteringCorrectionsHandoff)
    stale = Mock(return_value="70\n30")  # corrected only while Pell's sender was stale or paused
    problem.app = SimpleNamespace(customer=Mock(return_value=""), core=stale, rows=lambda run, q: files)
    facts = {
        "pell_gap": [100, 200],
        "pell_gap_from": 90,
        "pell_gap_corrected": [40],
        "pell_stale_through": 5,
        "statement_return": {"genuine": 50, "missing": 51},
    }
    pell = {"extra": [30, 70], "missing": [95, 150, 51], "changed": [40]}
    assert problem.seal_pell_classes(facts, pell) == []
    assert facts["pell_base"] == [70]  # 30 is stale too, but its delivered file keeps it a residual
    assert facts["pell_classes"]["advance_granularity"] == [40, 95, 150]
    assert facts["pell_classes"]["loader_rebook"] == [30, 31, 40]
    pell["changed"].append(60)
    assert problem.seal_pell_classes(facts, pell) == ["Pell residual 60 is in no class"]
