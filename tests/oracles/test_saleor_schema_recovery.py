import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from sregym.conductor.oracles.base import Oracle
from sregym.conductor.problems.ia_ports import saleor_b


@pytest.fixture
def state(tmp_path, monkeypatch):
    baseline = {"1": {"id": 1, "postal_code": "10001", "phone": None}}
    path = tmp_path / "schema-drift.json"
    path.write_text(json.dumps({"addresses": baseline}))
    monkeypatch.setattr(saleor_b, "state_file", lambda *_: path)
    checkout = Mock(return_value=None)
    monkeypatch.setattr(saleor_b, "require_checkout", checkout)
    problem = object.__new__(saleor_b.SchemaColumnDriftSaleor)
    problem._columns = Mock(return_value={"id", "postal_code", "phone"})
    problem._address_snapshot = Mock(return_value=baseline)
    return problem, path, checkout


@pytest.mark.parametrize(
    "addresses",
    [
        {},
        {"2": {"id": 2, "postal_code": "10001", "phone": None}},
        {"1": {"id": 1, "postal_code": "99999", "phone": None}},
        {"1": {"id": 1, "postal_code": "10001"}},
    ],
)
def test_original_address_identity_and_contents_are_required(state, addresses):
    problem, _, checkout = state
    problem._address_snapshot.return_value = addresses
    result = problem.fault_check(SimpleNamespace(fail=Oracle.fail))
    assert result["success"] is False
    assert result["reason"] == "original_addresses_changed"
    checkout.assert_not_called()


def test_new_addresses_and_additional_columns_are_allowed(state):
    problem, _, checkout = state
    problem._address_snapshot.return_value = {
        "1": {"id": 1, "postal_code": "10001", "phone": None, "extra": "new"},
        "2": {"id": 2, "postal_code": "90210"},
    }
    assert problem.fault_check(SimpleNamespace(fail=Oracle.fail)) is None
    checkout.assert_called_once()


def test_missing_baseline_does_not_accept_an_empty_table(state):
    problem, path, checkout = state
    path.unlink()
    result = problem.fault_check(SimpleNamespace(fail=Oracle.fail))
    assert result["success"] is False
    assert result["reason"] == "address_baseline_unavailable"
    checkout.assert_not_called()


def test_missing_column_fails_before_reading_address_contents(state):
    problem, _, checkout = state
    problem._columns.return_value = {"id", "postcode"}
    result = problem.fault_check(SimpleNamespace(fail=Oracle.fail))
    assert result["success"] is False
    problem._address_snapshot.assert_not_called()
    checkout.assert_not_called()
