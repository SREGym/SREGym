"""Check causal recurrence, financial integrity and original delivery identity."""

import copy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from sregym.conductor.oracles.stripe_config_recovery import StripeConfigRecoveryOracle, compare_state
from sregym.service.apps.incident_runtime import feature_edge as edge


@pytest.fixture
def state():
    return {
        "customers": {"cus_1": {"id": "cus_1"}},
        "payment_intents": {"pi_1": {"amount": 2500}},
        "charges": {"ch_1": {"customer": "cus_1", "amount_captured": 2500}},
        "refunds": {"re_1": {"charge": "ch_1", "amount": 100}},
        "webhook_endpoints": {"we_1": {"url": "http://edge/hook"}},
        "events": {
            "evt_1": {
                "id": "evt_1",
                "type": "payment_intent.succeeded",
                "created": 1,
                "data": {"object": {"id": "pi_1"}},
            }
        },
        "deliveries": {"evt_1": [{"status": "succeeded"}]},
    }


def report(original, actual, receipts=None):
    expected = {k: v for k, v in original.items() if k not in ("events", "deliveries")}
    return compare_state(
        expected, actual, original["events"], list(original["events"].values()) if receipts is None else receipts
    )


def test_deleting_queue_or_reissuing_notification_cannot_hide_missing_event(state):
    assert not report(state, state)["undelivered_events"]
    actual = copy.deepcopy(state)
    actual["events"].clear()
    assert report(state, actual)["missing_events"] == ["evt_1"]
    assert report(state, state, [])["undelivered_events"] == ["evt_1"]


@pytest.mark.parametrize("collection", ["customers", "payment_intents", "charges", "refunds"])
def test_business_data_loss_fails(state, collection):
    actual = copy.deepcopy(state)
    actual[collection].clear()
    assert report(state, actual)["changed_records"][collection] == 1


def test_recreating_charge_or_refund_is_not_recovery(state):
    actual = copy.deepcopy(state)
    actual["charges"]["ch_duplicate"] = dict(state["charges"]["ch_1"])
    actual["refunds"]["re_duplicate"] = dict(state["refunds"]["re_1"])
    assert report(state, actual)["duplicate_financial_effects"] == 2


def test_good_current_file_does_not_hide_next_bad_publication(tmp_path, monkeypatch):
    monkeypatch.setattr(edge, "CONTROL", tmp_path)
    edge.write("current.json", {"features": edge.CANONICAL})
    edge.write("settings.json", {"enabled": True, "bot_management_enabled": True, "sources": ["old", "new"]})
    monkeypatch.setattr(edge, "features", lambda source: edge.CANONICAL * (2 if source == "new" else 1))
    assert not edge.safety()["safe"]
    edge.write("settings.json", {"enabled": False, "bot_management_enabled": True, "sources": ["old", "new"]})
    assert edge.safety()["safe"]


def test_empty_or_truncated_configuration_is_not_valid_containment(tmp_path, monkeypatch):
    monkeypatch.setattr(edge, "CONTROL", tmp_path)
    edge.write("current.json", {"features": []})
    edge.write("settings.json", {"enabled": False, "bot_management_enabled": True, "sources": []})
    assert not edge.safety()["safe"]
    edge.write("settings.json", {"enabled": True, "bot_management_enabled": False, "sources": []})
    assert edge.safety()["safe"]  # Explicitly allowed emergency kill switch.


def test_recurring_risk_is_decisive_without_mutating_business_state():
    app = Mock()
    app.configuration_safety.return_value = {"safe": False}
    oracle = StripeConfigRecoveryOracle(SimpleNamespace(app=app, expected_state={}))
    oracle.baseline = {"token": "abc"}
    assert oracle.evaluate()["reason"] == "recurring_configuration_risk"
    app.run_client.assert_not_called()


@pytest.mark.parametrize("answers,missing", [(["t"], "state_table"), (["f", "0"], "state_row")])
def test_erasing_durable_state_is_a_decisive_failure(answers, missing):
    app = Mock()
    app.configuration_safety.return_value = {"safe": True}
    app.database_pods.return_value = [{"metadata": {"name": "db-1"}}]
    app.sql.side_effect = answers
    oracle = StripeConfigRecoveryOracle(SimpleNamespace(app=app, expected_state={}))
    oracle.baseline = {"token": "abc"}
    result = oracle.evaluate()
    assert result["reason"] == "acknowledged_payment_state_changed"
    assert result["failure_class"] == "agent_error"
    assert result["detail"]["missing"] == missing


@pytest.mark.parametrize("pending_queue", [True, False])
def test_received_notification_with_remaining_delivery_work_is_incomplete(state, pending_queue):
    actual = copy.deepcopy(state)
    if pending_queue:
        actual["delivery_jobs"] = {"evt_1:we_1": {"event": "evt_1"}}
    else:
        actual["events"]["evt_1"]["pending_webhooks"] = 1
    assert report(state, actual)["undelivered_events"] == ["evt_1"]
