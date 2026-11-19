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
