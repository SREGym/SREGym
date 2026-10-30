import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from sregym.conductor.oracles.apps.gitea.mysql_failover import (
    GiteaMySQLFailoverOracle,
    delivery_report,
    duplicated_comments,
    issue_events,
    live_traffic_report,
    search_queries,
    stale_deliveries,
)
from sregym.conductor.oracles.service_endpoint_mitigation import ServiceEndpointMitigationOracle
from sregym.service.apps.gitea.mysql_failover import LIVE_TRAFFIC, RECEIVER, RELAY


def issue(id, index, name, content="b", closed=False, created=0):
    return {
        "id": id,
        "repo_id": 5,
        "index": index,
        "name": name,
        "content": content,
        "is_closed": closed,
        "created_unix": created,
    }


def state(issues, comments=()):
    return {
        "users": [{"id": 1, "lower_name": "zoo"}],
        "repositories": [{"id": 5, "owner_id": 1, "lower_name": "pets"}],
        "issues": issues,
        "comments": [{"id": c, "issue_id": i, "content": body} for c, i, body in comments],
    }


def receipt(title, body="b", **update):
    return {"repository": "Zoo/Pets", "number": 1, "title": title, "body": body, **update}


def delivery(event, action, number, issue_id, title, body="b", comment_id=None, comment=None):
    return {
        "delivery": f"{title}-{action}-{comment_id}",
        "event": event,
        "action": action,
        "repository": "Zoo/Pets",
        "number": number,
        "issue_id": issue_id,
        "title": title,
        "body": body,
        "state": "closed" if action == "closed" else "open",
        "comment_id": comment_id,
        "comment": comment,
    }


PRIMARY = state([issue(10, 1, "a"), issue(11, 2, "b", closed=True)], comments=[(7, 10, "hi")])
UPDATES = [receipt("a", comment="hi"), receipt("b", close=True)]
DELIVERED = [
    delivery("issues", "opened", 1, 10, "a"),
    delivery("issues", "opened", 2, 11, "b"),
    delivery("issue_comment", "created", 1, 10, "a", comment_id=7, comment="hi"),
    delivery("issues", "closed", 2, 11, "b"),
]


def test_every_acknowledged_write_maps_to_the_primarys_current_row():
    events, lost = issue_events(PRIMARY, [receipt("a"), receipt("b"), receipt("c")], UPDATES)
    assert lost == ["c"]
    assert ("issues", "opened", "zoo/pets", 2, 11, "b", "b", None, None) in events
    assert ("issue_comment", "created", "zoo/pets", 1, 10, "a", "b", 7, "hi") in events


def test_comments_and_closes_missing_on_the_primary_are_lost():
    reopened = state([issue(10, 1, "a"), issue(11, 2, "b")])
    assert issue_events(reopened, [], UPDATES)[1] == ["hi", "b"]


def test_each_event_must_arrive_exactly_once():
    events = issue_events(PRIMARY, [receipt("a"), receipt("b")], UPDATES)[0]
    assert delivery_report(DELIVERED, events) == {"undelivered": [], "duplicated": [], "conflicting": []}
    report = delivery_report(DELIVERED[:2] + DELIVERED[1:3], events)
    assert report["undelivered"] == ["b"] and report["duplicated"] == ["b"]


def test_a_replayed_payload_under_a_reused_number_conflicts():
    events = issue_events(PRIMARY, [receipt("a"), receipt("b")], UPDATES)[0]
    stale = delivery("issues", "opened", 2, 31, "orphan", body="x")
    assert delivery_report([*DELIVERED, stale], events)["conflicting"] == ["Zoo/Pets#2"]


@pytest.mark.parametrize("closed_state", ["open", None, ""])
def test_a_close_event_must_describe_a_closed_issue(closed_state):
    events = issue_events(PRIMARY, [receipt("a"), receipt("b")], UPDATES)[0]
    delivered = [*DELIVERED[:-1], {**DELIVERED[-1], "state": closed_state}]
    assert delivery_report(delivered, events)["conflicting"] == ["Zoo/Pets#2"]


def test_a_close_event_without_its_issue_state_conflicts():
    events = issue_events(PRIMARY, [receipt("a"), receipt("b")], UPDATES)[0]
    closed = {k: v for k, v in DELIVERED[-1].items() if k != "state"}
    assert delivery_report([*DELIVERED[:-1], closed], events)["conflicting"] == ["Zoo/Pets#2"]


@pytest.mark.parametrize("opened_state", ["open", "closed"])
def test_an_open_event_can_precede_its_issue_closure(opened_state):
    events = issue_events(PRIMARY, [receipt("a"), receipt("b")], UPDATES)[0]
    delivered = [DELIVERED[0], {**DELIVERED[1], "state": opened_state}, *DELIVERED[2:]]
    assert delivery_report(delivered, events) == {"undelivered": [], "duplicated": [], "conflicting": []}


def test_a_signed_event_naming_the_wrong_number_does_not_count():
    events = issue_events(PRIMARY, [receipt("a")], [])[0]
    forged = delivery("issues", "opened", 9, 10, "a")
    assert delivery_report([forged], events)["undelivered"] == ["a"]


def test_a_comment_posted_twice_on_the_primary_is_duplicated():
    twice = state([issue(10, 1, "a"), issue(11, 2, "b", closed=True)], comments=[(7, 10, "hi"), (8, 10, "hi")])
    assert duplicated_comments(twice, issue_events(twice, [], UPDATES)[0]) == ["hi"]
    assert duplicated_comments(PRIMARY, issue_events(PRIMARY, [], UPDATES)[0]) == []


def test_the_same_comment_on_another_issue_is_not_a_duplicate():
    current = state([issue(10, 1, "a"), issue(11, 2, "b")], comments=[(7, 10, "hi"), (8, 11, "hi")])
    events, lost = issue_events(current, [], [receipt("a", comment="hi")])
    assert not lost
    assert duplicated_comments(current, events) == []


def test_recovered_comment_identity_can_change_and_preserves_expected_multiplicity():
    current = state([issue(30, 9, "a")], comments=[(70, 30, "hi"), (80, 30, "hi")])
    events, lost = issue_events(current, [], [receipt("a", comment="hi")] * 2)
    assert not lost
    assert duplicated_comments(current, events) == []
    assert duplicated_comments(current, events[:1]) == ["hi"]


def test_a_delivery_the_primary_does_not_back_is_stale():
    old_comment = delivery("issue_comment", "created", 1, 10, "a", comment_id=3, comment="hi")
    vacant = delivery("issues", "opened", 9, 40, "gone")
    assert stale_deliveries(PRIMARY, DELIVERED) == []
    assert stale_deliveries(PRIMARY, [*DELIVERED, old_comment, vacant]) == ["a-created-3", "gone-opened-None"]


def test_an_event_the_primary_sent_stays_valid_after_its_issue_changes():
    vacant = delivery("issues", "opened", 9, 40, "gone")
    assert stale_deliveries(PRIMARY, [*DELIVERED, vacant], recorded={"gone-opened-None"}) == []


def traffic(*writes, started=0, now=20):
    return {
        "started": started,
        "now": now,
        "acknowledged": [{"number": n, "title": t, "body": "b", "at": at} for n, t, at in writes],
    }


def test_live_writes_must_survive_on_the_primary():
    issues = [issue(1, 4, "f1", created=5), issue(2, 5, "f2", created=10)]
    report = live_traffic_report(traffic((4, "f1", 5), (5, "f2", 10), (6, "f3", 15)), issues, 5)
    assert report == {"lost": ["f3"], "longest_outage": 5}


def test_a_live_write_with_its_body_changed_is_lost():
    report = live_traffic_report(traffic((4, "f1", 5)), [issue(1, 4, "f1", content="", created=5)], 5)
    assert report["lost"] == ["f1"]


def test_a_write_stored_long_after_its_acknowledgement_is_lost():
    report = live_traffic_report(traffic((4, "f1", 5)), [issue(1, 4, "f1", created=40)], 5)
    assert report["lost"] == ["f1"]


def test_a_stretch_without_acknowledged_writes_is_an_outage():
    assert live_traffic_report(traffic((4, "f1", 5), now=65), [issue(1, 4, "f1", created=5)], 5)["longest_outage"] == 60


def test_only_the_oracles_own_pauses_are_excused():
    writes = traffic((4, "f1", 5), now=65)
    assert live_traffic_report(writes, [issue(1, 4, "f1", created=5)], 5, graded=[(10, 62)])["longest_outage"] == 8


@pytest.fixture
def resource_oracle():
    app = Mock()
    app.mysql.write_service = "gitea-db-write"
    app.command.return_value = json.dumps(
        {
            "spec": {
                "replicas": 1,
                "template": {
                    "spec": {
                        "containers": [{"env": [{"name": "GITEA__database__HOST", "value": "gitea-db-write:3306"}]}]
                    }
                },
            }
        }
    )
    identities = {RECEIVER: [("receiver", 0)], LIVE_TRAFFIC: [("users", 0)], RELAY: [("new-relay", 0)]}
    app.pod_identities.side_effect = identities.__getitem__
    problem = SimpleNamespace(
        app=app, receiver=identities[RECEIVER], live_users=identities[LIVE_TRAFFIC], relay=[("old-relay", 0)]
    )
    oracle = GiteaMySQLFailoverOracle(problem)
    oracle.volumes = {"gitea-repositories": "repository-volume"}
    oracle.volume_ids = Mock(return_value=dict(oracle.volumes))
    return oracle, identities


def test_a_replacement_relay_does_not_fail_the_resource_check(resource_oracle):
    oracle, _ = resource_oracle
    assert oracle.check_resources() is None
    assert RELAY not in [call.args[0] for call in oracle.problem.app.pod_identities.call_args_list]


@pytest.mark.parametrize(
    "resource,reason", [(RECEIVER, "webhook_receiver_replaced"), (LIVE_TRAFFIC, "live_traffic_replaced")]
)
def test_replacing_protected_evidence_still_fails(resource_oracle, resource, reason):
    oracle, identities = resource_oracle
    identities[resource] = [("replacement", 0)]
    assert oracle.check_resources()["reason"] == reason


def test_a_replacement_relay_does_not_excuse_lost_events(resource_oracle):
    oracle, _ = resource_oracle
    problem, app = oracle.problem, oracle.problem.app
    problem.shared = PRIMARY
    problem.shared_receipts = [receipt("a"), receipt("b")]
    problem.orphaned = []
    problem.accepted_after_promotion = []
    problem.updates = UPDATES
    problem.repository_ids = {"Zoo/Pets": 5}
    app.snapshot.return_value = PRIMARY
    app.wait_for_relay.return_value = {"paused": False, "queued": []}
    app.webhook_deliveries.return_value = DELIVERED[:-1]
    app.delivery_ids.return_value = set()

    assert oracle.check_resources() is None
    verdict = oracle.check_divergence("west", None)
    assert verdict["reason"] == "webhook_events_undelivered"
    assert verdict["detail"]["events"] == ["b"]


HEALTHY = {"source": "uuid-west", "io": "ON", "sql": "ON"}


def mysql(
    writable=("west",),
    pods=("pod/gitea-db-west-0",),
    replication=HEALTHY,
    applies=True,
    consistent=True,
):
    m = Mock()
    m.writable.side_effect = lambda region: region in writable
    m.pod.side_effect = lambda region: f"gitea-db-{region}-0"
    m.write_pods.return_value = list(pods)
    m.server_uuid.side_effect = lambda region: f"uuid-{region}"
    m.replication.return_value = replication
    m.applies_everything.return_value = applies
    m.consistent.return_value = consistent
    return m


def oracle(m, counters_behind=0, statistics_stale=0):
    app = SimpleNamespace(
        mysql=m,
        issue_counters_behind=lambda region: counters_behind,
        issue_statistics_stale=lambda region: statistics_stale,
    )
    return GiteaMySQLFailoverOracle(SimpleNamespace(app=app))


def test_repaired_replication_passes():
    assert oracle(mysql()).check_replication() == ("west", None)


@pytest.mark.parametrize("primary,replica", [("west", "east"), ("east", "west")])
@pytest.mark.parametrize("after_restart", [None, {"success": False, "reason": "writable_primary_count"}])
def test_grading_rechecks_replication_after_restarting_only_the_replica(primary, replica, after_restart):
    problem = SimpleNamespace(app=Mock(scale_tier="small"), shared=None, orphaned=[], accepted_after_promotion=[])
    check = GiteaMySQLFailoverOracle(problem)
    check.baseline = {}
    check.check_resources = Mock(return_value=None)
    check.check_replication = Mock(side_effect=[(primary, None), (primary, after_restart)])
    with patch.object(ServiceEndpointMitigationOracle, "evaluate", return_value={"success": True}):
        result = check.grade(None)
    problem.app.mysql.restart.assert_called_once_with(replica)
    assert check.check_replication.call_count == 2
    if after_restart is not None:
        assert result == after_restart
    else:
        assert result["success"] is True


def test_grading_does_not_restart_a_replica_to_repair_an_invalid_submission():
    problem = SimpleNamespace(app=Mock(), shared=None)
    check = GiteaMySQLFailoverOracle(problem)
    check.check_resources = Mock(return_value=None)
    failure = {"success": False, "reason": "replica_not_replicating"}
    check.check_replication = Mock(return_value=("west", failure))
    with patch.object(ServiceEndpointMitigationOracle, "evaluate", return_value={"success": True}):
        assert check.grade(None) == failure
    problem.app.mysql.restart.assert_not_called()


@pytest.mark.parametrize(
    "state,reason",
    [
        # No-op after failover: west is primary and east is not replicating.
        ({"replication": None}, "replica_not_replicating"),
        ({"writable": ("east", "west")}, "writable_primary_count"),
        ({"writable": ()}, "writable_primary_count"),
        ({"pods": ("pod/gitea-db-east-0",)}, "write_service_not_on_primary"),
        ({"pods": ("pod/gitea-db-west-0", "pod/rogue")}, "write_service_not_on_primary"),
        ({"replication": {"source": "uuid-west", "io": "ON", "sql": "OFF"}}, "replica_not_replicating"),
        ({"replication": {"source": "uuid-other", "io": "ON", "sql": "ON"}}, "replica_not_replicating"),
        ({"replication": {"channels": 2}}, "replica_not_replicating"),
        ({"applies": False}, "replica_skips_changes"),
        ({"consistent": False}, "regions_inconsistent"),
    ],
)
def test_broken_replication_is_named(state, reason):
    assert oracle(mysql(**state)).check_replication()[1]["reason"] == reason


def test_issues_inserted_without_advancing_the_counter_fail():
    assert oracle(mysql(), counters_behind=2).check_replication()[1]["reason"] == "issue_counter_behind"


def test_comments_or_closes_written_without_updating_counts_fail():
    assert oracle(mysql(), statistics_stale=1).check_replication()[1]["reason"] == "issue_statistics_stale"


def test_stranded_writes_are_searched_under_their_current_numbers():
    orphan = {**receipt("b"), "number": 9, "reference": "r1"}
    comment = {**receipt("a"), "comment": "hi", "reference": "r2"}
    queries = search_queries(PRIMARY, [orphan], [comment, receipt("b", close=True)])
    assert queries == [
        {"repository": "Zoo/Pets", "text": "r1", "number": 2},
        {"repository": "Zoo/Pets", "text": "r2", "number": 1},
    ]
