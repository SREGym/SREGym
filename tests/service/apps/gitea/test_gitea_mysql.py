import base64
import json
import subprocess
from unittest.mock import Mock

import pytest

from sregym.service.apps.components.mysql import MySQLRegions, checked_gtids
from sregym.service.apps.gitea.mysql_failover import (
    CHECKS,
    DATABASE_CLIENT,
    DIVERGENCE_COUNTS,
    HIDDEN,
    LIVE_TRAFFIC,
    MEMBER_SECRET,
    RECEIVER,
    RELAY,
    GiteaMySQLApplication,
    undeliverable,
)


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
    assert env["GITEA__webhook__ALLOWED_HOST_LIST"] == RELAY
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
    receiver = next(d for d in by_kind(GiteaMySQLApplication().receiver_documents(), "Deployment"))["spec"]
    assert receiver["template"]["metadata"]["labels"]["region"] == "west"
    assert receiver["template"]["spec"]["automountServiceAccountToken"] is False


def test_relay_queues_beside_gitea_and_forwards_across_regions():
    app = GiteaMySQLApplication()
    assert not any(d["metadata"]["name"] in (RECEIVER, RELAY) for d in app.render())
    documents = app.relay_documents("http://10.96.0.9/")
    relay = next(d for d in by_kind(documents, "Deployment"))["spec"]["template"]
    assert relay["metadata"]["labels"]["region"] == "east"
    env = {e["name"]: e["value"] for e in relay["spec"]["containers"][0]["env"]}
    assert env["TARGET"] == "http://10.96.0.9/"
    assert [d["metadata"]["name"] for d in by_kind(documents, "Service")] == [RELAY]


def test_the_consumer_and_the_users_run_where_the_agent_cannot_see():
    app = GiteaMySQLApplication()
    app.command = Mock(return_value=json.dumps({"items": []}))
    for name in (RECEIVER, LIVE_TRAFFIC, CHECKS, DATABASE_CLIENT):
        app.pod_identities(name)
        assert app.command.call_args.args[:2] == ("-n", HIDDEN)
    app.pod_identities(RELAY)
    assert app.command.call_args.args[0] == "get"


def test_queued_events_that_describe_the_old_history_or_repeat_are_undeliverable():
    state = {
        "users": [{"id": 1, "lower_name": "zoo"}],
        "repositories": [{"id": 5, "owner_id": 1, "lower_name": "pets"}],
        "issues": [
            {"id": 1, "repo_id": 5, "index": 1, "name": "shared", "content": "a", "is_closed": False},
            {"id": 3, "repo_id": 5, "index": 2, "name": "post", "content": "c", "is_closed": False},
        ],
        "comments": [{"id": 7, "issue_id": 1, "content": "seen in west"}],
    }

    def event(id, number, issue_id, title, body, action="opened", comment_id=None, comment=None):
        return {
            "id": id,
            "repository": "Zoo/Pets",
            "action": action,
            "number": number,
            "issue_id": issue_id,
            "title": title,
            "body": body,
            "comment_id": comment_id,
            "comment": comment,
        }

    queued = [
        event(1, 2, 3, "post", "c"),
        event(2, 2, 2, "orphan", "b"),
        event(3, 1, 1, "shared", "a", "closed"),
        event(4, 1, 1, "shared", "a", "created", 7, "seen in west"),
        event(5, 1, 1, "shared", "a", "created", 7, "seen in east"),
        event(6, 9, 4, "gone", "d"),
        event(7, 2, 3, "post", "c"),
    ]
    assert undeliverable(state, queued) == [2, 3, 5, 6, 7]


def test_tiers_differ_only_in_divergence_size():
    assert GiteaMySQLApplication("small").divergence_counts == DIVERGENCE_COUNTS["small"]
    assert GiteaMySQLApplication("large").divergence_counts == DIVERGENCE_COUNTS["large"]
    assert GiteaMySQLApplication().expected_volume_count == 3
    with pytest.raises(ValueError):
        GiteaMySQLApplication("replicated")


def test_partition_denies_only_cross_region_traffic():
    east, west = MySQLRegions(Mock(), "gitea-db", "gitea", "x", "s").partition_documents("gitea")
    assert east["spec"]["selector"] == "region == 'east'"
    deny = {"selector": "region == 'west'", "namespaceSelector": "all()"}
    assert east["spec"]["ingress"][0] == {"action": "Deny", "source": deny}
    assert east["spec"]["egress"][0] == {"action": "Deny", "destination": deny}
    assert east["spec"]["ingress"][-1] == {"action": "Allow"}
    assert west["spec"]["selector"] == "region == 'west'"


def test_queries_reach_each_server_by_address_from_an_outside_client():
    command = Mock(return_value="10.244.1.7")
    client = Mock(return_value="1\n")
    mysql = MySQLRegions(command, "gitea-db", "gitea", "x", "s", client=(client, "db-client"))
    assert mysql.sql("west", "SELECT 1;") == "1"
    assert command.call_args.args[:2] == ("get", "pod")
    assert client.call_args.args[:3] == ("exec", "-i", "deployment/db-client")
    assert "10.244.1.7" in client.call_args.args


def test_unreachable_target_is_reported_rather_than_raised():
    command = Mock(side_effect=subprocess.CalledProcessError(1, ["mysqladmin"]))
    assert MySQLRegions(command, "gitea-db", "gitea", "x", "s").reachable("west", "east") is False


def test_initialize_leaves_an_initialized_pair_alone():
    command = Mock(return_value="1")
    MySQLRegions(command, "gitea-db", "gitea", "x", "s").initialize("ab")
    assert command.call_count == 1


def test_missing_or_starting_pods_do_not_raise():
    app = GiteaMySQLApplication()
    app.command = Mock(return_value=json.dumps({"items": [{"metadata": {"uid": "u"}, "status": {}}]}))
    assert app.pod_identities("gitea") == [("u", 0)]


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
    assert {"name": "credentials", "secret": {"secretName": MEMBER_SECRET}} in spec["spec"]["volumes"]


def test_description_states_requirements_but_not_the_regions_or_the_fault():
    for tier in DIVERGENCE_COUNTS:
        description = GiteaMySQLApplication(tier).description
        assert not any(word in description for word in ("east", "west", "webhook", "partition", "Tier", tier))
        assert all(word in description for word in ("replica", "available", "exactly once"))


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
    app.relay = Mock(return_value={"paused": True, "queued": []})
    monkeypatch.setattr("sregym.service.apps.gitea.mysql_failover.time.sleep", lambda seconds: None)
    app.reference_repair()
    assert app.relay.call_args_list[-1].args == ("resume",)
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
