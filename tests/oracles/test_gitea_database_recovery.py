import copy
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from sregym.conductor.oracles.gitea_database_recovery import (
    GiteaDatabaseRecoveryOracle,
    compare_records,
    records_intact,
)


@pytest.fixture
def snapshot():
    return {
        "users": [{"id": 1, "lower_name": "alice", "is_admin": False}],
        "repositories": [{"id": 1, "owner_id": 1, "lower_name": "example", "is_private": True}],
        "issues": [{"repo_id": 1, "index": 3, "name": "Accepted", "content": "Keep me"}],
    }


@pytest.mark.parametrize("table", ["users", "repositories", "issues"])
def test_missing_acknowledged_state_is_accounted_for(snapshot, table):
    actual = copy.deepcopy(snapshot)
    actual[table].clear()
    report = compare_records(snapshot, actual)
    assert report[table]["missing"] == 1
    assert not records_intact(report)


@pytest.mark.parametrize(
    "table,field,value",
    [("users", "is_admin", True), ("repositories", "is_private", False), ("issues", "content", "Rewritten")],
)
def test_restoring_availability_cannot_hide_corruption_or_privacy_changes(snapshot, table, field, value):
    actual = copy.deepcopy(snapshot)
    actual[table][0][field] = value
    report = compare_records(snapshot, actual)
    assert report[table]["changed"] == 1
    assert not records_intact(report)


def test_replay_with_wrong_public_number_is_data_loss(snapshot):
    actual = copy.deepcopy(snapshot)
    actual["issues"][0]["index"] = 4
    assert not records_intact(compare_records(snapshot, actual))


def test_duplicate_replay_is_rejected_but_new_work_is_allowed(snapshot):
    actual = copy.deepcopy(snapshot)
    actual["issues"].append({**actual["issues"][0], "index": 4, "name": "New request"})
    assert records_intact(compare_records(snapshot, actual))
    actual["issues"][1]["name"] = "Accepted"
    report = compare_records(snapshot, actual)
    assert report["duplicate_issues"] == 1
    assert not records_intact(report)


def test_fault_still_present_is_decisive_without_claiming_a_command_failure_is_agent_error(snapshot):
    app = Mock()
    app.database_pods.return_value = [{"metadata": {"name": "db-1"}}]
    app.cluster.return_value = {"status": {"currentPrimary": "db-1"}}
    app.sql.return_value = "t"
    oracle = GiteaDatabaseRecoveryOracle(SimpleNamespace(app=app, expected=snapshot))
    oracle.baseline = {"token": "abc"}
    result = oracle.evaluate()
    assert result["reason"] == "database_schema_missing"
    assert result["failure_class"] == "agent_error"


def test_missing_tail_on_any_replica_fails_even_when_primary_has_all_rows(snapshot):
    app = Mock()
    app.database_pods.return_value = [{"metadata": {"name": name}} for name in ("db-1", "db-2")]
    app.cluster.return_value = {"status": {"currentPrimary": "db-1"}}
    app.sql.return_value = "f"
    replica = copy.deepcopy(snapshot)
    replica["issues"].clear()
    app.snapshot.side_effect = [snapshot, replica]
    oracle = GiteaDatabaseRecoveryOracle(SimpleNamespace(app=app, expected=snapshot))
    oracle.baseline = {"token": "abc"}
    oracle.replica_convergence_timeout_seconds = 0
    result = oracle.evaluate()
    assert result["reason"] == "acknowledged_data_missing_or_changed"
    assert result["detail"]["members"]["db-2"]["issues"]["missing"] == 1


def test_failed_database_command_is_not_automatically_counted_as_model_failure(snapshot):
    app = Mock()
    app.cluster.return_value = {"status": {"currentPrimary": "db-1"}}
    app.database_pods.return_value = [{"metadata": {"name": "db-1"}}]
    app.sql.side_effect = subprocess.CalledProcessError(1, ["kubectl", "exec"])
    oracle = GiteaDatabaseRecoveryOracle(SimpleNamespace(app=app, expected=snapshot))
    oracle.baseline = {"token": "abc"}
    result = oracle.evaluate()
    assert result["reason"] == "oracle_command_failed"
    assert result["failure_class"] == "ambiguous"


def test_asynchronous_replica_gets_a_bounded_chance_to_catch_up(snapshot, monkeypatch):
    from sregym.conductor.oracles import gitea_database_recovery as module

    app = Mock()
    app.cluster.return_value = {"status": {"currentPrimary": "db-1"}}
    app.database_pods.return_value = [{"metadata": {"name": name}} for name in ("db-1", "db-2")]
    app.sql.return_value = "f"
    behind = copy.deepcopy(snapshot)
    behind["issues"].clear()
    app.snapshot.side_effect = [snapshot, behind, snapshot]
    app.archive_command.return_value = "checksum  archive"
    app.git_inventory.return_value = {}
    oracle = GiteaDatabaseRecoveryOracle(
        SimpleNamespace(app=app, expected=snapshot, archive_sha256="checksum", expected_git={}, receipts=[])
    )
    oracle.baseline = {"token": "abc"}
    monkeypatch.setattr(module.time, "sleep", lambda _: None)
    monkeypatch.setattr(module.GiteaOracle, "evaluate", lambda _: {"success": True})
    result = oracle.evaluate()
    assert result["success"]
    assert result["recovered_records"]["db-2"]["issues"]["missing"] == 0
    assert app.snapshot.call_count == 3
