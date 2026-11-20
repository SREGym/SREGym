from decimal import Decimal

from sregym.conductor.oracles.apps.metering.corrections_handoff import differences, digest_rows
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
