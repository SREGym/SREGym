import copy
import sqlite3
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from sregym.conductor.oracles.gitea import GiteaOracle


def oracle_with_database():
    cluster = {
        "spec": {
            "instances": 3,
            "postgresql": {"synchronous": {"method": "any", "number": 1, "dataDurability": "required"}},
        },
        "status": {"readyInstances": 3, "currentPrimary": "db-1", "targetPrimary": "db-1"},
    }
    app = Mock(members=3)
    app.cluster.return_value = copy.deepcopy(cluster)
    app.database_pods.return_value = [
        {"metadata": {"name": f"db-{n}"}, "status": {"conditions": [{"type": "Ready", "status": "True"}]}}
        for n in range(1, 4)
    ]
    # Match Gitea v1.27.3 models/issues/issue.go: Title maps to xorm:"name".
    # Execute the data query so an invalid column cannot pass through a mock.
    database = sqlite3.connect(":memory:")
    database.execute("CREATE TABLE issue (name TEXT)")
    database.execute("INSERT INTO issue VALUES ('sregym-abc123')")

    def sql(statement, pod):
        if "pg_is_in_recovery" in statement:
            return "t" if pod == "db-1" else "f"
        return str(database.execute(statement).fetchone()[0])

    app.sql.side_effect = sql
    return GiteaOracle(SimpleNamespace(app=app)), app


@pytest.mark.parametrize("admission_default", [False, True])
def test_replication_reads_the_acknowledged_issue_from_every_member(admission_default):
    oracle, app = oracle_with_database()
    if admission_default:
        app.cluster.return_value["spec"]["postgresql"]["synchronous"]["failoverQuorum"] = False
    oracle.verify_replication("abc123")
    reads = [c.kwargs["pod"] for c in app.sql.call_args_list if "FROM issue" in c.args[0]]
    assert reads == ["db-1", "db-2", "db-3"]


def test_weakened_write_quorum_is_rejected():
    oracle, app = oracle_with_database()
    app.cluster.return_value["spec"]["postgresql"]["synchronous"]["dataDurability"] = "preferred"
    with pytest.raises(RuntimeError, match="synchronous replication was changed"):
        oracle.verify_replication("abc123")
    app.sql.assert_not_called()


def test_split_brain_is_rejected_even_if_all_members_have_the_issue():
    oracle, app = oracle_with_database()
    app.sql.side_effect = lambda statement, pod: "t" if "pg_is_in_recovery" in statement else "1"
    with pytest.raises(RuntimeError, match="exactly one writable"):
        oracle.verify_replication("abc123")


def test_oracle_requires_preincident_baseline():
    oracle, _ = oracle_with_database()
    assert oracle.evaluate()["reason"] == "baseline_not_captured"


def test_stale_operator_readiness_cannot_hide_an_unready_replica():
    oracle, app = oracle_with_database()
    app.database_pods.return_value[1]["status"]["conditions"][0]["status"] = "False"
    with pytest.raises(RuntimeError, match="Database pods are not ready"):
        oracle.verify_replication("abc123")
    app.sql.assert_not_called()
