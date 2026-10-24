from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from sregym.conductor.oracles.apps.gitea.mysql_failover import (
    GiteaMySQLFailoverOracle,
    delivery_report,
    issue_events,
    live_traffic_report,
    search_queries,
)


def issue(id, index, name, content="b", closed=False):
    return {"id": id, "repo_id": 5, "index": index, "name": name, "content": content, "is_closed": closed}


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


def test_a_signed_event_naming_the_wrong_number_does_not_count():
    events = issue_events(PRIMARY, [receipt("a")], [])[0]
    forged = delivery("issues", "opened", 9, 10, "a")
    assert delivery_report([forged], events)["undelivered"] == ["a"]


def traffic(*writes, started=0, now=20):
    return {
        "started": started,
        "now": now,
        "acknowledged": [{"number": n, "title": t, "at": at} for n, t, at in writes],
    }


def test_live_writes_must_survive_on_the_primary():
    issues = [issue(1, 4, "f1"), issue(2, 5, "f2")]
    report = live_traffic_report(traffic((4, "f1", 5), (5, "f2", 10), (6, "f3", 15)), issues, 5)
    assert report == {"lost": ["f3"], "longest_outage": 5}


def test_a_stretch_without_acknowledged_writes_is_an_outage():
    assert live_traffic_report(traffic((4, "f1", 5), now=65), [issue(1, 4, "f1")], 5)["longest_outage"] == 60


def test_only_the_oracles_own_pauses_are_excused():
    writes = traffic((4, "f1", 5), now=65)
    assert live_traffic_report(writes, [issue(1, 4, "f1")], 5, graded=[(10, 62)])["longest_outage"] == 8


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


@pytest.mark.parametrize(
    "state,reason",
    [
        # No-op after failover: west is primary but east is fenced, not replicating.
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
