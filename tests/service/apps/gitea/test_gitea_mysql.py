import base64
import json
import subprocess
from unittest.mock import Mock

import pytest

from sregym.service.apps.components.mysql import MySQLRegions, checked_gtids
from sregym.service.apps.gitea.mysql_failover import DIVERGENCE_COUNTS, LIVE_TRAFFIC, RECEIVER, GiteaMySQLApplication


def by_kind(documents, kind):
    return [d for d in documents if d["kind"] == kind]


def test_gitea_writes_through_the_routed_service_and_runs_in_east():
    documents = GiteaMySQLApplication().render()
    assert not by_kind(documents, "Cluster")
    gitea = next(d for d in by_kind(documents, "Deployment") if d["metadata"]["name"] == "gitea")["spec"]
    env = {e["name"]: e.get("value") for e in gitea["template"]["spec"]["containers"][0]["env"]}
    assert env["GITEA__database__DB_TYPE"] == "mysql"
    assert env["GITEA__database__HOST"] == "gitea-db-rw:3306"
    assert "GITEA__database__SSL_MODE" not in env
    assert env["GITEA__webhook__ALLOWED_HOST_LIST"] == RECEIVER
    assert gitea["replicas"] == 1
    assert gitea["template"]["metadata"]["labels"]["region"] == "east"


def test_each_region_has_one_mysql_server_and_writes_start_in_east():
    documents = GiteaMySQLApplication().render()
    statefulsets = {d["metadata"]["name"]: d["spec"] for d in by_kind(documents, "StatefulSet")}
    assert set(statefulsets) == {"gitea-db-east", "gitea-db-west"}
    for region, spec in (("east", statefulsets["gitea-db-east"]), ("west", statefulsets["gitea-db-west"])):
        assert spec["replicas"] == 1
        assert spec["template"]["metadata"]["labels"] == {"app": "gitea-db", "region": region}
        assert "--gtid-mode=ON" in spec["template"]["spec"]["containers"][0]["args"]
    services = {d["metadata"]["name"]: d["spec"] for d in by_kind(documents, "Service")}
    assert services["gitea-db-rw"]["selector"] == {"app": "gitea-db", "region": "east"}
    receiver = next(d for d in by_kind(documents, "Deployment") if d["metadata"]["name"] == RECEIVER)["spec"]
    assert receiver["template"]["metadata"]["labels"]["region"] == "west"
    assert receiver["template"]["spec"]["automountServiceAccountToken"] is False


def test_tiers_differ_only_in_divergence_size():
    assert GiteaMySQLApplication("small").divergence_counts == DIVERGENCE_COUNTS["small"]
    assert GiteaMySQLApplication("large").divergence_counts == DIVERGENCE_COUNTS["large"]
    assert GiteaMySQLApplication().expected_volume_count == 3
    with pytest.raises(ValueError):
        GiteaMySQLApplication("replicated")


def test_partition_denies_only_cross_region_traffic():
    east, west = MySQLRegions(Mock(), "gitea-db", "gitea", "x", "s").partition_documents("gitea")
    assert east["spec"]["selector"] == "region == 'east'"
    assert east["spec"]["ingress"][0] == {"action": "Deny", "source": {"selector": "region == 'west'"}}
    assert east["spec"]["egress"][0] == {"action": "Deny", "destination": {"selector": "region == 'west'"}}
    assert east["spec"]["ingress"][-1] == {"action": "Allow"}
    assert west["spec"]["selector"] == "region == 'west'"


def test_unreachable_target_is_reported_rather_than_raised():
    command = Mock(side_effect=subprocess.CalledProcessError(1, ["mysqladmin"]))
    assert MySQLRegions(command, "gitea-db", "gitea", "x", "s").reachable("west", "east") is False


def test_gtid_sets_are_checked_before_interpolation():
    assert checked_gtids("3e11fa47-71ca-11e1-9e33-c80aa9429562:1-5") == "3e11fa47-71ca-11e1-9e33-c80aa9429562:1-5"
    with pytest.raises(ValueError):
        checked_gtids("1'; DROP DATABASE gitea; --")


def encode(value):
    return json.dumps({"data": {"key": base64.b64encode(value.encode()).decode()}})


def test_secret_values_must_be_generated_hex():
    app = GiteaMySQLApplication()
    app.command = Mock(return_value=encode("00ff"))
    assert app.secret_value("s", "key") == "00ff"
    app.command = Mock(return_value=encode("x' OR '1"))
    with pytest.raises(ValueError):
        app.secret_value("s", "key")


def test_live_users_write_through_gitea_from_east_only():
    documents = GiteaMySQLApplication().live_traffic_documents()
    assert not any(d["metadata"]["name"] == LIVE_TRAFFIC for d in GiteaMySQLApplication().render())
    spec = next(d for d in documents if d["kind"] == "Deployment")["spec"]["template"]
    assert spec["metadata"]["labels"]["region"] == "east"
    assert {"name": "credentials", "secret": {"secretName": "gitea-admin"}} in spec["spec"]["volumes"]


def test_description_does_not_describe_the_topology():
    for tier in DIVERGENCE_COUNTS:
        description = GiteaMySQLApplication(tier).description
        assert not any(word in description for word in ("east", "west", "replica", "webhook", "Tier", tier))


def test_reference_repair_replays_only_unreplicated_activity_then_rebuilds(monkeypatch):
    mysql = Mock()
    mysql.write_region.return_value = "west"
    monkeypatch.setattr(GiteaMySQLApplication, "mysql", property(lambda self: mysql))
    app = GiteaMySQLApplication()
    users = [{"id": 1, "lower_name": "zoo"}]
    repositories = [{"id": 5, "owner_id": 1, "lower_name": "pets"}]

    def issue(id, index, name, content, closed=False):
        return {"id": id, "repo_id": 5, "index": index, "name": name, "content": content, "is_closed": closed}

    snapshots = {
        "west": {
            "users": users,
            "repositories": repositories,
            "issues": [issue(1, 1, "shared", "a"), issue(2, 2, "post", "c")],
            "comments": [{"id": 1, "issue_id": 1, "content": "seen in west"}],
        },
        "east": {
            "users": users,
            "repositories": repositories,
            "issues": [issue(1, 1, "shared", "a", closed=True), issue(2, 2, "orphan", "b")],
            "comments": [
                {"id": 1, "issue_id": 1, "content": "seen in east"},
                {"id": 2, "issue_id": 2, "content": "orphan note"},
            ],
        },
    }
    app.snapshot = lambda region=None: snapshots[region]
    app.recovery_client = Mock(
        side_effect=lambda mode, **arguments: [{**arguments["records"][0], "number": 3}] if mode == "create" else []
    )
    app.secret_value = Mock(return_value="ab")
    app.reference_repair()
    assert app.recovery_client.call_args_list[0].kwargs == {
        "records": [{"repository": "zoo/pets", "title": "orphan", "body": "b"}]
    }
    assert app.recovery_client.call_args_list[1].kwargs == {
        "updates": [
            {"repository": "zoo/pets", "number": 1, "comment": "seen in east"},
            {"repository": "zoo/pets", "number": 1, "close": True},
            {"repository": "zoo/pets", "number": 3, "comment": "orphan note"},
        ]
    }
    mysql.rebuild.assert_called_once_with("east", "west", "ab")
