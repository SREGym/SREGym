"""Public identity, access control, and acknowledged history are safety invariants."""

import copy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from sregym.conductor.oracles.gitlab_database_recovery import (
    GitLabDatabaseRecoveryOracle,
    compare_records,
    records_intact,
)
from sregym.service.apps.gitlab_recovery_workflow import reconcile


@pytest.fixture
def snapshot():
    return {
        "users": [{"id": 1, "username": "alice", "email": "alice@example.org", "admin": False, "state": "active"}],
        "projects": [{"id": 1, "path": "private", "visibility_level": 0}],
        "members": [{"id": 1, "source_id": 1, "user_id": 1, "access_level": 30}],
        "issues": [{"project_id": 1, "iid": 3, "title": "Accepted", "description": "Keep me", "confidential": True}],
    }


@pytest.mark.parametrize(
    "table,field,value",
    [
        ("users", "admin", True),
        ("projects", "visibility_level", 20),
        ("members", "access_level", 50),
        ("issues", "confidential", False),
        ("issues", "iid", 4),
        ("issues", "description", "rewritten"),
    ],
)
def test_availability_does_not_hide_corruption(snapshot, table, field, value):
    actual = copy.deepcopy(snapshot)
    actual[table][0][field] = value
    assert not records_intact(compare_records(snapshot, actual))


def test_new_work_allowed_but_duplicate_replay_rejected(snapshot):
    actual = copy.deepcopy(snapshot)
    actual["issues"].append({**actual["issues"][0], "iid": 4, "title": "New work"})
    assert records_intact(compare_records(snapshot, actual))
    actual["issues"][1]["title"] = "Accepted"
    assert compare_records(snapshot, actual)["duplicate_issues"] == 1


@pytest.mark.parametrize("table", ["users", "projects", "members", "issues"])
def test_missing_acknowledged_state_is_loss(snapshot, table):
    actual = copy.deepcopy(snapshot)
    actual[table].clear()
    assert not records_intact(compare_records(snapshot, actual))


def test_missing_tail_on_replica_fails(snapshot):
    app = Mock()
    app.cluster.return_value = {"status": {"currentPrimary": "db-1"}}
    app.database_pods.return_value = [{"metadata": {"name": n}} for n in ("db-1", "db-2")]
    app.sql.return_value = "f"
    behind = copy.deepcopy(snapshot)
    behind["issues"].clear()
    app.snapshot.side_effect = [snapshot, behind]
    oracle = GitLabDatabaseRecoveryOracle(SimpleNamespace(app=app, expected=snapshot))
    oracle.baseline = {"token": "abc"}
    oracle.replica_convergence_timeout_seconds = 0
    result = oracle.evaluate()
    assert result["reason"] == "acknowledged_data_missing_or_changed"
    assert result["detail"]["members"]["db-2"]["issues"]["missing"] == 1


def test_replay_never_overwrites_conflicting_identity():
    receipt = {"project": 1, "iid": 2, "title": "A", "description": "B", "confidential": True}
    client = Mock()
    client.call.return_value = {**receipt, "title": "Someone else's issue"}
    with pytest.raises(RuntimeError, match="conflicting"):
        reconcile(client, [receipt], write=True)
    assert all(call.args[0] == "GET" for call in client.call.call_args_list)


def test_replay_skips_existing_and_does_not_treat_server_errors_as_missing():
    receipt = {"project": 1, "iid": 2, "title": "A", "description": "B", "confidential": True}
    client = Mock()
    client.call.return_value = receipt
    assert reconcile(client, [receipt], write=True)["created"] == 0
    client.call.side_effect = RuntimeError("HTTP 500: unavailable")
    with pytest.raises(RuntimeError, match="500"):
        reconcile(client, [receipt], write=True)
