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
    ORCHESTRATOR,
    RECEIVER,
    RELAY,
    GiteaMySQLApplication,
    undeliverable,
)
from sregym.service.runtime_images import PYMYSQL_CLIENT_IMAGE

pytestmark = pytest.mark.usefixtures("offline_kubectl")


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


@pytest.mark.parametrize(
    "filters,modes,expected",
    [
        ("0", "OFF\tSTRICT", True),
        ("1", "OFF\tSTRICT", False),
        ("0", "1062\tSTRICT", False),
        ("0", "OFF\tIDEMPOTENT", False),
    ],
)
def test_only_active_filters_and_skipped_changes_fail(filters, modes, expected):
    mysql = MySQLRegions(Mock(), "gitea-db", "gitea", "x", "s")
    mysql.sql = Mock(side_effect=[filters, modes])
    assert mysql.applies_everything("east") is expected
    assert "WHERE FILTER_RULE <> ''" in mysql.sql.call_args_list[0].args[1]


def test_restart_waits_for_a_new_ready_pod_and_propagates_errors():
    command = Mock()
    mysql = MySQLRegions(command, "gitea-db", "gitea", "x", "s")
    mysql.restart("east", timeout=20)
    assert [call.args[:2] for call in command.call_args_list] == [
        ("delete", "pod/gitea-db-east-0"),
        ("wait", "--for=create"),
        ("wait", "--for=condition=Ready"),
    ]
    command.reset_mock()
    command.side_effect = subprocess.CalledProcessError(1, ["kubectl", "delete"])
    with pytest.raises(subprocess.CalledProcessError):
        mysql.restart("east")
    assert command.call_count == 1


def test_initialize_leaves_an_initialized_pair_alone():
    command = Mock(return_value="1")
    MySQLRegions(command, "gitea-db", "gitea", "x", "s").initialize("ab")
    assert command.call_count == 1


@pytest.mark.parametrize("stderr", ["mysqldump: connection failed", ""])
def test_rebuild_logs_dump_import_stderr_and_preserves_failure(caplog, stderr):
    error = subprocess.CalledProcessError(1, ["kubectl", "exec"], stderr=stderr)
    mysql = MySQLRegions(Mock(side_effect=error), "gitea-db", "gitea", "x", "s")
    mysql.sql = Mock()
    mysql.replicate = Mock()

    with pytest.raises(subprocess.CalledProcessError) as caught:
        mysql.rebuild("east", "west", "replication-password")

    assert caught.value is error
    assert "MySQL rebuild of east from west failed (exit 1)" in caplog.text
    assert (stderr or "(empty)") in caplog.text
    assert "replication-password" not in caplog.text
    mysql.replicate.assert_not_called()


def test_rebuild_success_still_reconfigures_and_checks_replication(caplog):
    command = Mock(return_value="")
    mysql = MySQLRegions(command, "gitea-db", "gitea", "x", "s")
    mysql.sql = Mock()
    mysql.replicate = Mock()
    mysql.converged = Mock(return_value=True)

    mysql.rebuild("east", "west", "ab")

    command.assert_called_once()
    assert command.call_args.args == (
        "exec",
        "gitea-db-east-0",
        "-c",
        "mysql",
        "--",
        "sh",
        "-c",
        'export MYSQL_PWD="$MYSQL_ROOT_PASSWORD"; set -o pipefail; '
        'mysqldump -h "$1" -uroot --single-transaction --set-gtid-purged=ON --databases "$2" '
        "| mysql --protocol=TCP -h127.0.0.1 -uroot",
        "rebuild",
        "gitea-db-west",
        "gitea",
    )
    assert command.call_args.kwargs == {"timeout": 900}
    mysql.replicate.assert_called_once_with("east", "west", "ab")
    mysql.converged.assert_called_once_with("east", "west")
    assert not caplog.records


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


def test_orchestrator_uses_prebuilt_pymysql_runtime_image_without_pip():
    documents = GiteaMySQLApplication().orchestrator_documents({"master": "gitea-db-east-0"})
    deployment = next(d for d in by_kind(documents, "Deployment") if d["metadata"]["name"] == ORCHESTRATOR)
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    assert container["image"] == PYMYSQL_CLIENT_IMAGE
    assert container["command"] == ["python", "-u", "/app/main.py"]
    assert "pip" not in json.dumps(container)


def test_description_states_requirements_but_not_the_regions_or_the_fault():
    for tier in DIVERGENCE_COUNTS:
        description = GiteaMySQLApplication(tier).description
        assert not any(word in description for word in ("east", "west", "webhook", "partition", "Tier", tier))
        assert all(word in description for word in ("replica", "available", "Orchestrator", "responsive"))


def test_reference_repair_fails_back_then_replays_only_unreplicated_activity(monkeypatch):
    steps = Mock()
    mysql = steps.mysql
    mysql.write_region.return_value = "west"
    mysql.pod.side_effect = lambda region: f"gitea-db-{region}-0"
    monkeypatch.setattr(GiteaMySQLApplication, "mysql", property(lambda self: mysql))
    app = GiteaMySQLApplication()
    users = [{"id": 1, "lower_name": "zoo"}, {"id": 2, "lower_name": "admin"}]
    repositories = [{"id": 5, "owner_id": 1, "lower_name": "pets"}]

    def issue(id, index, name, content, closed=False, milestone=0):
        return {
            "id": id,
            "repo_id": 5,
            "index": index,
            "name": name,
            "content": content,
            "is_closed": closed,
            "milestone_id": milestone,
        }

    west = {
        "users": users,
        "repositories": repositories,
        "issues": [issue(1, 1, "shared", "a"), issue(2, 2, "post", "c")],
        "comments": [{"id": 1, "issue_id": 1, "content": "seen in west"}],
        "labels": [{"issue_id": 1, "name": "bug"}],
        "assignees": [],
        "milestones": [{"id": 7, "name": "2026.11"}],
        "reactions": [],
    }
    east = {
        "users": users,
        "repositories": [*repositories, {"id": 6, "owner_id": 1, "lower_name": "notes"}],
        "issues": [issue(1, 1, "shared", "a", closed=True, milestone=7), issue(2, 2, "orphan", "b")],
        "comments": [
            {"id": 1, "issue_id": 1, "content": "seen in east"},
            {"id": 2, "issue_id": 2, "content": "orphan note"},
        ],
        "labels": [{"issue_id": 1, "name": "bug"}, {"issue_id": 1, "name": "regression"}],
        "assignees": [{"issue_id": 2, "user": "zoo"}],
        "milestones": [{"id": 7, "name": "2026.11"}],
        "reactions": [{"issue_id": 2, "type": "heart", "user": "admin"}, {"issue_id": 2, "type": "+1", "user": "zoo"}],
    }
    app.snapshot = lambda region=None: east if region == "east" else west
    app.recovery_client = steps.recovery_client
    app.recovery_client.side_effect = lambda mode, **arguments: (
        [{**arguments["records"][0], "number": 3}] if mode == "create" else []
    )
    app.secret_value = Mock(return_value="ab")
    app.relay = steps.relay
    app.queued_events = Mock(return_value=[])
    app.orchestrator = steps.orchestrator
    app.sync_branches = steps.sync_branches
    app.redeliver_stuck_webhooks = steps.redeliver_stuck_webhooks
    app.admin_username = Mock(return_value="admin")
    monkeypatch.setattr("sregym.service.apps.gitea.mysql_failover.time.sleep", lambda seconds: None)
    app.reference_repair()

    steps_taken = (
        "orchestrator",
        "relay",
        "mysql.rebuild",
        "recovery_client",
        "sync_branches",
        "redeliver_stuck_webhooks",
    )
    order = [c[0] for c in steps.mock_calls if c[0] in steps_taken]
    assert order == [
        "orchestrator",
        "relay",
        "mysql.rebuild",
        "orchestrator",
        "orchestrator",
        "recovery_client",
        "recovery_client",
        "recovery_client",
        "sync_branches",
        "redeliver_stuck_webhooks",
        "relay",
    ]
    assert [c.args[0] for c in steps.orchestrator.call_args_list] == [
        "disable-global-recoveries",
        "graceful-master-takeover-auto/gitea/gitea-db-east-0/3306",
        "enable-global-recoveries",
    ]
    assert [c.args for c in steps.relay.call_args_list] == [("pause",), ("resume",)]
    mysql.rebuild.assert_called_once_with("east", "west", "ab")
    adopt, create, update = steps.recovery_client.call_args_list
    assert adopt.kwargs == {"repositories": ["zoo/notes"]}
    assert create.kwargs == {"records": [{"repository": "zoo/pets", "title": "orphan", "body": "b"}]}
    shared = {"repository": "zoo/pets", "title": "shared", "body": "a", "number": 1}
    orphan = {"repository": "zoo/pets", "title": "orphan", "body": "b", "number": 3}
    assert update.kwargs == {
        "updates": [
            {**shared, "comment": "seen in east"},
            {**shared, "close": True},
            {**shared, "label": "regression"},
            {**shared, "milestone": "2026.11"},
            {**orphan, "comment": "orphan note"},
            {**orphan, "assignee": "zoo"},
            {**orphan, "reaction": "heart"},
        ]
    }
