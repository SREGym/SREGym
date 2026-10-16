"""Focused checks for Shuffle's reference, payment, and mitigation rules."""

import hashlib
import hmac
import json
import os
import subprocess
import sys
import tempfile
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

import pytest

from sregym.conductor.oracles.mitigation import MitigationOracle
from sregym.conductor.problems.shuffle_scale_out import OrderShop, ShuffleMitigation, ShuffleScaleOut

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "docker" / "apps" / "shuffle"))
os.environ.setdefault("DATABASE_URL", "postgresql://unused/unused")


def _stub_fixture_dependencies():
    """The in-cluster harness needs psycopg and confluent-kafka, which SREGym does not install."""
    try:
        import_module("psycopg")
    except ImportError:
        sys.modules["psycopg"] = SimpleNamespace(
            connect=MagicMock(), OperationalError=type("OperationalError", (Exception,), {})
        )
    try:
        import_module("confluent_kafka")
    except ImportError:
        kafka = SimpleNamespace(
            Consumer=MagicMock(),
            Producer=MagicMock(),
            TopicPartition=MagicMock(),
            KafkaException=type("KafkaException", (Exception,), {}),
        )
        sys.modules["confluent_kafka"] = kafka
        sys.modules["confluent_kafka.admin"] = SimpleNamespace(
            AdminClient=MagicMock(), NewPartitions=MagicMock(), NewTopic=MagicMock()
        )


_stub_fixture_dependencies()
harness = import_module("harness")
provider = import_module("provider")


def test_helper_image_has_dependencies_but_no_fixture_code():
    image = os.environ.get("SHUFFLE_HELPER_IMAGE_TEST")
    if not image:
        pytest.skip("Set SHUFFLE_HELPER_IMAGE_TEST to verify the helper image")
    probe = """from pathlib import Path
import importlib.util, psycopg, confluent_kafka
assert importlib.util.find_spec('harness') is None
assert not list(Path('/app').rglob('*')) if Path('/app').exists() else True
"""
    subprocess.run(["docker", "run", "--rm", image, "python", "-c", probe], check=True, timeout=30)


def test_lifecycle_recovery_resets_application_and_removes_hidden_fixture():
    problem = object.__new__(ShuffleScaleOut)
    problem.app = MagicMock()
    problem.kubectl = MagicMock()
    problem.namespace = "astronomy-shop"
    problem.recover_fault()
    problem.app.cleanup.assert_called_once_with()
    problem.kubectl.wait_for_namespace_deletion.assert_called_once_with("astronomy-shop")
    problem.app.deploy.assert_called_once_with()
    assert problem._recovered_baseline is True

    app = object.__new__(OrderShop)
    app.kubectl = MagicMock()
    with patch("sregym.service.apps.astronomy_shop.AstronomyShop.cleanup") as base_cleanup:
        app.cleanup()
    base_cleanup.assert_called_once_with()
    assert "pvc/provider-data" in app.kubectl.exec_command.call_args.args[0]


def test_lifecycle_baseline_check_does_not_change_incident_grading():
    problem = SimpleNamespace(_recovered_baseline=True, app=MagicMock())
    problem.app.helper.side_effect = [
        {"settled": True},
        {"success": True},
        {"success": True},
    ]
    oracle = ShuffleMitigation(problem)
    report = oracle.evaluate()
    assert report["success"] is True
    assert set(report["phases"]) == {"baseline_traffic", "baseline_history", "baseline_provider"}
    assert problem.app.helper.call_args_list[0].args == ("traffic", "baseline", "128")


def test_drain_uses_business_application_not_consumer_group():
    db = MagicMock()
    db.__enter__.return_value = db
    db.execute.return_value = [("create",), ("cancel",)]
    records = [{"body": json.dumps(event)} for event in ({"ID": "create"}, {"data": {"ID": "cancel"}})]
    with (
        patch.object(harness, "audit", return_value={"records": records}),
        patch.object(harness, "database", return_value=db),
    ):
        harness.drain(timeout=1)
    assert "shop.applied_events" in db.execute.call_args.args[0]


def test_stuck_consumer_fails_drain():
    db = MagicMock()
    db.__enter__.return_value = db
    db.execute.return_value = []
    with (
        patch.object(harness, "audit", return_value={"records": [{"body": '{"ID":"stuck"}'}]}),
        patch.object(harness, "database", return_value=db),
        pytest.raises(RuntimeError, match="1 pending"),
    ):
        harness.drain(timeout=0.01)


def test_capacity_requires_distinct_skus():
    meter = harness.EligibilityMeter()
    meter.reset(["first", "second"])
    with meter.count("first"), meter.count("first"), meter.count("second"), meter.count("other"):
        assert meter.peak() == 2


def test_reconciliation_applies_cancellations_before_compensation():
    events = [{"Order": str(i), "SKU": "sku", "Kind": "create", "Version": 2**53 + i} for i in range(6)]
    events.append({"Order": "0", "Kind": "cancel", "Version": 2**53 + 7})
    required, cases = harness.history_plan(events, dict.fromkeys(map(str, range(6)), "reserved"), {"sku": (4, True)})
    assert required["0"] == "cancelled"
    assert required["4"] == "reserved"
    assert required["5"] == "compensated"
    assert cases[0]["excess_after_cancellation"] == 1


def test_recovery_merges_signed_archive_and_kafka():
    old = {"ID": "old", "Order": "one", "Kind": "create", "Version": 2**53 + 1}
    new = {"ID": "new", "Order": "one", "Kind": "cancel", "Version": 2**53 + 2}

    def record(document):
        body = json.dumps(document)
        return {
            "body": body,
            "signature": hmac.new(b"key", body.encode(), hashlib.sha256).hexdigest(),
            "topic": harness.SOURCE,
        }

    with (
        patch.dict(harness.os.environ, {"EVENT_KEY": "key"}),
        patch.object(harness, "audit", return_value={"archive": [record(old)], "records": [record({"data": new})]}),
    ):
        assert harness.commands() == [new, old]


def test_live_slo_rejects_maintenance_window():
    rows = [dict(accepted_at=1, applied_at=26, error=None), dict(accepted_at=10, applied_at=27, error=None)]
    assert not harness.live_slo(rows, 0, 28)["success"]
    assert harness.live_slo([dict(accepted_at=1, applied_at=2, error=None)], 0, 3)["success"]


@pytest.fixture
def provider_db():
    with (
        tempfile.TemporaryDirectory() as directory,
        patch.object(provider, "DATA", Path(directory) / "provider.sqlite"),
    ):
        provider.initialize()
        yield


def test_provider_key_expires_and_ambiguous_503_duplicates(provider_db):
    first = {"order_id": "one", "cents": 100, "revision": 1}
    second = {"order_id": "two", "cents": 100, "revision": 2}
    status, result = provider.post("/refunds", first, "refund:1", now=100)
    assert status == 200
    assert provider.post("/refunds", second, "refund:1", now=101) == (200, result)
    assert provider.post("/refunds", second, "refund:1", now=100 + provider.RETENTION + 1)[1]["id"] != result["id"]
    with patch.object(provider.time, "time", return_value=200):
        provider.disruption(seconds=10, count=1)
    assert provider.post("/refunds", first, "refund:2", now=200)[0] == 503
    assert provider.post("/refunds", first, "refund:2", now=201)[0] == 503
    assert provider.post("/refunds", first, "refund:2", now=200 + provider.RETENTION + 1)[0] == 200


def test_adjustment_exactly_once(provider_db):
    row = provider.post("/refunds", {"order_id": "one", "cents": 100, "revision": 1}, "refund:1", now=100)[1]
    body = {"refund_id": row["id"]}
    result = provider.post("/adjustments", body, "adjust:1", now=101)
    assert result[0] == 200
    assert provider.post("/adjustments", body, "adjust:1", now=102) == result
    assert provider.post("/adjustments", body, "adjust:2", now=103)[0] == 409


def mitigation(helper):
    app = Mock()
    app.helper.side_effect = helper
    return ShuffleMitigation(SimpleNamespace(app=app)), app


def good_helper(*args):
    if args[0] == "traffic":
        return {"settled": True, "rate": 70, "max_independent_inflight": 6}
    if args[0] == "pause":
        return {"settled": True}
    return {"success": True}


@pytest.mark.parametrize("rate,overlap,passes", [(39, 6, False), (70, 3, False), (40, 4, True)])
def test_capacity_requires_rate_and_independent_overlap(rate, overlap, passes):
    def helper(*args):
        if args[:2] == ("traffic", "capacity"):
            return {"settled": True, "rate": rate, "max_independent_inflight": overlap}
        return good_helper(*args)

    oracle, _ = mitigation(helper)
    with patch.object(MitigationOracle, "evaluate", return_value={"success": True}):
        result = oracle.evaluate()
    assert result["phases"]["traffic"]["success"] is passes
    assert result["success"] is passes


@pytest.mark.parametrize("failed", ["pause", "check", "payment-check"])
def test_early_failure_does_not_skip_history_or_provider(failed):
    def helper(*args):
        if args[0] == failed:
            raise TimeoutError(failed)
        return good_helper(*args)

    oracle, _ = mitigation(helper)
    with patch.object(MitigationOracle, "evaluate", return_value={"success": True}):
        result = oracle.evaluate()
    assert not result["success"]
    assert "history" in result["phases"] and "provider" in result["phases"]
    assert "persistence" in result["phases"]


def test_restart_loss_fails_persistence():
    def helper(*args):
        if args[:2] == ("traffic", "cancellation"):
            return {"settled": False, "rate": 70, "max_independent_inflight": 6}
        return good_helper(*args)

    oracle, app = mitigation(helper)
    with patch.object(MitigationOracle, "evaluate", return_value={"success": True}):
        result = oracle.evaluate()
    assert not result["phases"]["persistence"]["success"]
    app.restart.assert_any_call("event-gateway")


def test_replay_catches_a_non_idempotent_repair():
    after_replay = False

    def helper(*args):
        nonlocal after_replay
        if args[0] == "replay":
            after_replay = True
        elif args[0] == "check" and after_replay:
            after_replay = False
            return {"success": False, "failure_count": 1}
        return good_helper(*args)

    oracle, _ = mitigation(helper)
    result = oracle.evaluate()
    assert result["phases"]["history"]["success"]
    assert not result["phases"]["replay"]["success"]
    assert result["phases"]["persistence"]["success"]
