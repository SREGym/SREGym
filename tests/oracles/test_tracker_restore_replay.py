"""Offline checks for the tracker restore replay problem (no cluster)."""

import copy

import pytest

from sregym.conductor.oracles.apps.tracker.restore_replay import (
    client_copy,
    judge,
    micros,
    served_issues,
    tracker_view,
    write_gaps,
)

T0 = "2026-01-24T04:00:00Z"
ALICE, BOB = "user-alice", "user-bob"


def issue(key, title, **over):
    entity = {
        "id": key,
        "workspaceId": 1,
        "teamId": "team-eng",
        "number": over.pop("number", 1),
        "title": title,
        "description": "",
        "state": "todo",
        "priority": 0,
        "assigneeId": None,
        "creatorId": ALICE,
        "createdAt": T0,
    }
    entity.update(over)
    return entity


def comment(key, issue_id, body, **over):
    entity = {"id": key, "workspaceId": 1, "issueId": issue_id, "userId": BOB, "body": body, "createdAt": T0}
    entity.update(over)
    return entity


def action(number, model, entity, kind="I", delta=None):
    return {
        "id": number,
        "workspaceId": 1,
        "model": model,
        "modelId": entity["id"],
        "action": kind,
        "data": entity,
        "delta": delta,
        "actorId": ALICE,
    }


class World:
    """A workspace after a restore, with the lost window put back correctly.

    At the handover: issue A and issue B (whose state a user had already
    changed again), and issue C already deleted by a user. Lost: a new issue
    L with a comment, a title and a description for A, a comment on A, a state
    for B, and a title and a comment for C. Since the handover a user has
    changed A's description and created issue U.
    """

    def __init__(self):
        a = issue("A", "a at the backup", number=1)
        b = issue("B", "b", number=2, state="in_progress")
        self.handover = {"issues": [a, b], "comments": [comment("c0", "A", "from the backup")]}
        lost_issue = issue("L", "created in the lost window", number=3, createdAt="2026-01-24T05:00:00Z")
        c = issue("C", "c", number=4)
        self.lost = [
            action(101, "issue", lost_issue),
            action(102, "issue", {**a, "title": "a, lost"}, "U", {"title": "a, lost"}),
            action(103, "issue", {**a, "description": "lost text"}, "U", {"description": "lost text"}),
            action(104, "comment", comment("c1", "A", "lost comment", createdAt="2026-01-24T05:10:00Z")),
            action(105, "comment", comment("c2", "L", "on the lost issue", createdAt="2026-01-24T05:11:00Z")),
            action(106, "issue", {**c, "title": "c, lost"}, "U", {"title": "c, lost"}),
            action(107, "comment", comment("c3", "C", "on the deleted issue")),
            action(108, "issue", {**b, "state": "done"}, "U", {"state": "done"}),
        ]
        made = issue("U", "made since the restore", number=3, createdAt="2026-01-24T11:00:00Z")
        self.writes = [
            self.write("issue_update", "B", fields={"state": "in_progress"}, phase="after"),
            self.write("issue_delete", "C", phase="after"),
            self.write("issue_create", "U", entity=made, phase="after"),
            self.write("issue_update", "A", fields={"description": "typed again"}, ended=200.0),
        ]
        restored = {**lost_issue, "number": 5}
        a_now = {**a, "title": "a, lost", "description": "typed again"}
        self.final = {
            "issues": [a_now, b, made, restored],
            "comments": [
                comment("c0", "A", "from the backup"),
                comment("c1", "A", "lost comment", createdAt="2026-01-24T05:10:00Z"),
                comment("c2", "L", "on the lost issue", createdAt="2026-01-24T05:11:00Z"),
            ],
        }
        # The handover copy already has U; the log is what the API wrote since.
        self.handover["issues"].append(made)
        self.log = [
            action(201, "issue", restored),
            action(202, "issue", {**a, "title": "a, lost"}, "U", {"title": "a, lost"}),
            action(203, "issue", a_now, "U", {"description": "typed again"}),
            action(204, "comment", self.final["comments"][1]),
            action(205, "comment", self.final["comments"][2]),
        ]
        self.listed = {(1, 106), (1, 107), (1, 108)}
        # No two of these share an entity, so a test changes exactly what it names.
        self.handover, self.lost = copy.deepcopy(self.handover), copy.deepcopy(self.lost)
        self.final, self.log = copy.deepcopy(self.final), copy.deepcopy(self.log)

    @staticmethod
    def write(kind, key, status="ok", phase="live", ended=100.0, **more):
        return {"ws": 1, "kind": kind, "id": key, "status": status, "phase": phase, "ended": ended, **more}

    def issue(self, key):
        return next(e for e in self.final["issues"] if e["id"] == key)

    def judge(self, served=None):
        return judge(
            tracker_view(self.handover),
            self.lost,
            self.writes,
            tracker_view(self.final),
            self.listed,
            self.log,
            served,
        )

    def served(self):
        """Every issue as tracker-api serves it, when it serves what the database holds."""
        return tracker_view(self.final)["issues"]


@pytest.fixture
def world():
    return World()


def test_a_correct_replay_has_no_problems(world):
    assert world.judge() == {}


def test_a_lost_change_written_over_a_users_change_is_found(world):
    world.issue("A")["description"] = "lost text"
    world.log[2]["data"]["description"] = "lost text"
    found = world.judge()
    assert list(found) == ["user_changes_overridden"]
    assert found["user_changes_overridden"]["sample"][0]["owed"] == "typed again"


def test_a_change_users_had_made_before_the_handover_also_stands(world):
    world.issue("B")["state"] = "done"
    world.log.append(action(206, "issue", world.issue("B"), "U", {"state": "done"}))
    assert list(world.judge()) == ["user_changes_overridden"]


def test_a_deleted_issue_brought_back_is_found(world):
    back = issue("C", "c, lost", number=4)
    world.final["issues"].append(back)
    world.log.append(action(206, "issue", back))
    found = world.judge()
    assert list(found) == ["user_changes_overridden"]
    assert "deleted" in found["user_changes_overridden"]["sample"][0]["what"]


def test_a_lost_issue_not_put_back_is_found_with_its_comment(world):
    world.final["issues"] = [e for e in world.final["issues"] if e["id"] != "L"]
    world.final["comments"] = [e for e in world.final["comments"] if e["id"] != "c2"]
    world.log = [a for a in world.log if a["id"] not in (201, 205)]
    found = world.judge()
    assert list(found) == ["lost_work_missing"]
    assert found["lost_work_missing"]["count"] == 2


def test_a_lost_field_not_put_back_is_found(world):
    world.issue("A")["title"] = "a at the backup"
    world.log = [a for a in world.log if a["id"] != 202]
    world.log[1]["data"]["title"] = "a at the backup"
    assert list(world.judge()) == ["lost_work_missing"]


def test_lost_work_put_back_under_another_name_or_time_is_found(world):
    world.final["comments"][1]["userId"] = ALICE
    world.log[3]["data"]["userId"] = ALICE
    assert list(world.judge()) == ["lost_work_missing"]
    world = World()
    world.issue("L")["createdAt"] = "2026-01-24T12:00:00Z"
    assert "lost_work_missing" in world.judge()


def test_a_lost_issue_may_take_a_new_number_but_others_keep_theirs(world):
    world.issue("L")["number"] = 9
    world.log[0]["data"]["number"] = 9
    assert world.judge() == {}
    world.issue("U")["number"] = 7
    assert "user_changes_overridden" in world.judge()


def test_something_nobody_created_is_found(world):
    extra = comment("c9", "A", "lost comment", createdAt="2026-01-24T05:10:00Z")
    world.final["comments"].append(extra)
    world.log.append(action(206, "comment", extra))
    assert list(world.judge()) == ["unexplained_changes"]


# ---- a lost comment its author posted again before the handover ---------------


def posted_again(world, status="ok", phase="after"):
    """Bob posts lost comment c1 again as c1b; the right repair leaves c1 out and lists it (lost action 104)."""
    again = comment("c1b", "A", "lost comment", createdAt="2026-01-24T10:30:00Z")
    more = {"entity": again, "issue": "A", "twin_of": "c1"}
    world.writes.insert(0, world.write("comment_create", "c1b", status=status, phase=phase, **more))
    if status == "ok":
        world.handover["comments"].append(again)
        world.final["comments"].append(again)
    return again


def leave_out(world, key, log_id):
    world.final["comments"] = [e for e in world.final["comments"] if e["id"] != key]
    world.log = [a for a in world.log if a["id"] != log_id]


def test_a_comment_posted_again_is_left_as_its_author_posted_it_and_listed(world):
    posted_again(world)
    leave_out(world, "c1", 204)
    world.listed.add((1, 104))
    assert world.judge() == {}


def test_a_comment_put_back_beside_the_one_its_author_posted_again_is_found(world):
    posted_again(world)
    found = world.judge()
    assert list(found) == ["lost_work_duplicated"]
    assert found["lost_work_duplicated"] == {"count": 1, "sample": [{"comment": "c1", "made_again_as": "c1b"}]}
    # Listing it does not make two of them right.
    world.listed.add((1, 104))
    assert list(world.judge()) == ["lost_work_duplicated"]


def test_a_comment_left_out_because_it_was_posted_again_must_be_listed(world):
    posted_again(world)
    leave_out(world, "c1", 204)
    found = world.judge()
    assert list(found) == ["skipped_not_listed"]
    assert found["skipped_not_listed"]["sample"] == [{"workspace": 1, "lost_action": 104}]


def test_removing_the_comment_its_author_posted_again_overrides_them(world):
    again = posted_again(world)
    world.final["comments"].remove(again)
    world.log.append(action(206, "comment", again, "D"))
    found = world.judge()
    assert list(found) == ["user_changes_overridden"]
    assert found["user_changes_overridden"]["sample"] == [{"comment": "c1b", "what": "missing"}]


def test_a_comment_whose_second_posting_was_rejected_is_still_owed(world):
    posted_again(world, status="failed")
    assert world.judge() == {}
    leave_out(world, "c1", 204)
    world.listed.add((1, 104))
    assert list(world.judge()) == ["lost_work_missing"]


def test_a_second_posting_of_unknown_outcome_leaves_both_readings_open(world):
    posted_again(world, status="unknown")
    assert world.judge() == {}
    leave_out(world, "c1", 204)
    assert world.judge() == {}


def created_again(world):
    """Alice creates lost issue L again as L2, with its number; the right repair leaves L out and lists it (101).

    L's lost comment c2 (105) then has no issue to go on; nothing is asked of it.
    """
    again = issue("L2", "created in the lost window", number=3, createdAt="2026-01-24T10:31:00Z")
    world.writes.insert(0, world.write("issue_create", "L2", phase="after", entity=again, twin_of="L"))
    made = world.issue("U")
    made["number"] = 6  # the issue users made since the restore came after it
    next(e for e in world.handover["issues"] if e["id"] == "U")["number"] = 6
    world.writes[3]["entity"] = made
    world.handover["issues"].append(again)
    world.final["issues"].append(again)
    return again


def test_an_issue_created_again_is_left_as_its_creator_made_it_and_listed(world):
    created_again(world)
    world.final["issues"] = [e for e in world.final["issues"] if e["id"] != "L"]
    world.final["comments"] = [e for e in world.final["comments"] if e["id"] != "c2"]
    world.log = [a for a in world.log if a["id"] not in (201, 205)]
    assert list(world.judge()) == ["skipped_not_listed"]
    world.listed.add((1, 101))
    assert world.judge() == {}


def test_an_issue_put_back_beside_the_one_its_creator_made_again_is_found(world):
    created_again(world)
    found = world.judge()
    assert list(found) == ["lost_work_duplicated"]
    assert found["lost_work_duplicated"]["sample"] == [{"issue": "L", "made_again_as": "L2"}]


def test_only_comments_posted_again_before_the_handover_count(world):
    # Users post nothing again once the agent has taken over; if one did, the lost comment is still owed.
    posted_again(world, phase="live")
    world.handover["comments"] = [e for e in world.handover["comments"] if e["id"] != "c1b"]
    world.log.append(action(206, "comment", world.final["comments"][-1]))
    assert world.judge() == {}


def test_a_field_nobody_changed_must_not_change(world):
    world.issue("B")["title"] = "edited by the replay"
    world.log.append(action(206, "issue", world.issue("B"), "U", {"title": "edited by the replay"}))
    assert list(world.judge()) == ["unexplained_changes"]


def test_what_could_not_be_put_back_must_be_listed(world):
    world.listed = {(1, 106)}
    found = world.judge()
    assert list(found) == ["skipped_not_listed"]
    assert {entry["lost_action"] for entry in found["skipped_not_listed"]["sample"]} == {107, 108}


def test_a_lost_change_a_user_typed_in_again_is_back_and_need_not_be_listed(world):
    # Lost: B's state set to done (108). Before the handover a user typed done in again.
    world.writes[0]["fields"] = {"state": "done"}
    for entity in (*world.handover["issues"], *world.final["issues"]):
        if entity["id"] == "B":
            entity["state"] = "done"
    world.listed.discard((1, 108))
    assert world.judge() == {}
    world.listed.add((1, 108))  # listing it all the same is no fault
    assert world.judge() == {}


def test_a_lost_value_typed_in_after_the_handover_also_counts_even_if_changed_again(world):
    # At the handover B's state is a user's in_progress; then a user types the lost done, and another todo.
    # Whoever reached 108 while the field held done had nothing to put back.
    world.writes.append(world.write("issue_update", "B", fields={"state": "done"}, ended=150.0))
    world.writes.append(world.write("issue_update", "B", fields={"state": "todo"}, ended=160.0))
    world.issue("B")["state"] = "todo"
    world.log.append(action(206, "issue", {**world.issue("B"), "state": "done"}, "U", {"state": "done"}))
    world.log.append(action(207, "issue", world.issue("B"), "U", {"state": "todo"}))
    world.listed.discard((1, 108))
    assert world.judge() == {}


def test_a_lost_value_no_user_typed_in_must_still_be_listed(world):
    # A user changes B's state again after the handover, but never to the lost done.
    world.writes.append(world.write("issue_update", "B", fields={"state": "canceled"}, ended=150.0))
    world.issue("B")["state"] = "canceled"
    world.log.append(action(206, "issue", world.issue("B"), "U", {"state": "canceled"}))
    assert world.judge() == {}
    world.listed.discard((1, 108))
    found = world.judge()
    assert list(found) == ["skipped_not_listed"]
    assert found["skipped_not_listed"]["sample"] == [{"workspace": 1, "lost_action": 108}]


def test_a_rejected_retyping_of_the_lost_value_does_not_count(world):
    world.writes.append(world.write("issue_update", "B", status="failed", fields={"state": "done"}, ended=150.0))
    world.listed.discard((1, 108))
    assert list(world.judge()) == ["skipped_not_listed"]


def test_a_conflict_that_arose_after_the_handover_need_not_be_listed(world):
    # 103 (A's description) lost to a change a user made after the handover.
    assert (1, 103) not in world.listed
    assert world.judge() == {}


def test_a_change_written_past_the_sync_log_never_reaches_users_apps(world):
    world.log = [a for a in world.log if a["id"] != 204]
    found = world.judge()
    assert list(found) == ["clients_out_of_sync"]
    assert found["clients_out_of_sync"]["sample"] == [{"comment": "c1"}]


def test_the_api_must_serve_what_the_database_holds(world):
    served = world.served()
    assert world.judge(served) == {}
    # A row changed behind tracker-api: it still serves the title it has in its cache.
    served["A"] = {**served["A"], "title": "a at the backup"}
    found = world.judge(served)
    assert list(found) == ["api_serves_stale_data"]
    assert found["api_serves_stale_data"]["sample"] == [{"issue": "A", "differs": ["title"]}]


def test_an_issue_the_api_does_not_serve_or_serves_without_a_row_is_found(world):
    served = world.served()
    del served["L"]
    served["gone"] = served["A"]
    found = world.judge(served)
    assert list(found) == ["api_serves_stale_data"]
    assert found["api_serves_stale_data"]["sample"] == [
        {"issue": "L", "differs": "not served"},
        {"issue": "gone", "differs": "served, but not in the database"},
    ]


def test_an_api_that_does_not_answer_its_users_serves_nothing(world):
    import subprocess
    from types import SimpleNamespace

    answers = [subprocess.CalledProcessError(1, "wget"), {"issues": world.final["issues"]}]

    def users_get(path):
        assert path == "/served"
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    app = SimpleNamespace(users_get=users_get)
    assert served_issues(app, wait=0) == world.served()  # asked again after a failure
    answers[:] = [subprocess.CalledProcessError(1, "wget")] * 3
    assert served_issues(app, wait=0) == {}
    found = world.judge({})
    assert list(found) == ["api_serves_stale_data"]
    assert found["api_serves_stale_data"]["count"] == len(world.final["issues"])
    assert found["api_serves_stale_data"]["sample"][0]["differs"] == "not served"


def test_a_write_whose_outcome_the_user_never_learned_may_go_either_way(world):
    world.writes.append(world.write("issue_update", "A", status="unknown", fields={"title": "maybe"}))
    assert world.judge() == {}
    world.issue("A")["title"] = "maybe"
    world.log.append(action(206, "issue", world.issue("A"), "U", {"title": "maybe"}))
    assert world.judge() == {}
    world.issue("A")["title"] = "something else"
    world.log[-1]["data"]["title"] = "something else"
    assert list(world.judge()) == ["lost_work_missing"]


def test_a_rejected_write_changes_nothing_users_are_owed(world):
    world.writes.append(world.write("issue_update", "A", status="failed", fields={"title": "rejected"}))
    assert world.judge() == {}


def test_a_users_app_follows_deletes_with_their_comments():
    handover = tracker_view({"issues": [issue("A", "a")], "comments": [comment("c0", "A", "x")]})
    copy_ = client_copy(handover, [action(5, "issue", issue("A", "a"), "D")])
    assert copy_ == {"issues": {}, "comments": {}}


def test_write_gaps_are_per_workspace_and_include_both_ends():
    writes = [
        {"ws": 1, "status": "ok", "ended": 103.0},
        {"ws": 1, "status": "ok", "ended": 105.0},
        {"ws": 1, "status": "failed", "ended": 110.0},
        {"ws": 1, "status": "ok", "ended": 131.0},
        {"ws": 2, "status": "unknown", "ended": 104.0},
        {"ws": 1, "status": "ok", "ended": 50.0},
    ]
    gaps = write_gaps(writes, start=100.0, end=140.0)
    assert gaps == {1: 26.0, 2: 40.0}


def test_a_stretch_in_which_a_grading_held_users_still_is_not_counted():
    writes = [{"ws": 1, "status": "ok", "ended": 105.0}, {"ws": 1, "status": "ok", "ended": 135.0}]
    assert write_gaps(writes, start=100.0, end=140.0) == {1: 30.0}
    assert write_gaps(writes, start=100.0, end=140.0, pauses=[(108.0, 130.0)]) == {1: 8.0}
    # Only the part of a pause inside a gap is taken off it.
    assert write_gaps(writes, start=100.0, end=140.0, pauses=[(90.0, 103.0), (138.0, 150.0)]) == {1: 30.0}


def test_timestamps_from_the_api_and_from_postgres_agree():
    assert micros("2026-01-24T05:10:00.123456Z") == micros("2026-01-24 05:10:00.123456+00")
    assert micros("2026-01-24T05:10:00.123456+00:00") == micros("2026-01-24T06:10:00.123456+01:00")
    assert micros("2026-01-24T05:10:00Z") % 1_000_000 == 0
    assert micros("2026-01-24T05:10:00.5Z") - micros("2026-01-24T05:10:00Z") == 500_000


def test_the_world_fixture_is_not_shared():
    first, second = World(), World()
    first.issue("A")["title"] = "changed"
    assert second.issue("A")["title"] == "a, lost"
    assert copy.deepcopy(first.final) == first.final


# ---- the application and the problem, without a cluster ----------------------


@pytest.fixture
def app(monkeypatch):
    from sregym.service.apps.tracker import application

    monkeypatch.setattr(application, "KubeCtl", lambda: None)
    monkeypatch.setenv("SREGYM_TRACKER_IMAGE", "app:dev")
    monkeypatch.setenv("SREGYM_TRACKER_HIDDEN_IMAGE", "hidden:dev")
    return application.TrackerApplication("small")


def test_description_states_expectations_not_the_method(app):
    import re

    from sregym.service.apps.tracker import application

    told = app.handover_description("09:56").lower()
    for expected in (
        "stay available and responsive",
        "comes back as it was, under their names and with its original times",
        "nothing they have done since",
        "whatever we do not put back, for whatever reason, is listed",
        "recovery page",
        "prerestore",
    ):
        assert expected in told, expected
    for hint in (r"if-match", r"\bcache\b", r"\bsql\b", r"x-user-id", r"createdat", r"truncate", r"migration"):
        assert not re.search(hint, told), hint
    assert "we cannot take tracker down again" in told
    application.STATE_NO_MORE_DOWNTIME = False
    try:
        assert "take tracker down again" not in app.handover_description("09:56").lower()
    finally:
        application.STATE_NO_MORE_DOWNTIME = True


def test_snapshots_carry_what_the_grader_reads(app):
    from sregym.service.apps.tracker import application

    for key in ("id", "workspaceId", "teamId", "number", "title", "description", "state", "priority", "assigneeId"):
        assert f"'{key}'" in application.ISSUE_JSON
    for key in ("id", "workspaceId", "issueId", "userId", "body", "createdAt"):
        assert f"'{key}'" in application.COMMENT_JSON
    seed = app.seed_sql()
    assert "generate_series(1, 8) w" in seed and "generate_series(1, 40) n" in seed
    assert seed.count("INSERT INTO sync_action") == 2


def test_users_make_lost_work_again_and_the_note_does_not_say_so(app):
    args = next(d for d in app.users_documents() if d["kind"] == "Deployment")["spec"]["template"]["spec"]
    assert "--reposts=3" in args["containers"][0]["args"]
    assert "--recreates=2" in args["containers"][0]["args"]
    hints = (
        "themselves",
        "again since",
        "posted again",
        "retyp",
        "re-post",
        "repost",
        "duplicate",
        "twice",
        "compare",
        "same title",
    )
    # The note says nothing about it: the agent has to see it in the data.
    told = app.handover_description("09:56").lower()
    assert not any(hint in told for hint in hints)


def test_manifests_keep_the_hidden_side_out_of_the_namespace(app):
    visible = app.database_documents() + app.api_documents() + app.migration_documents()
    assert all("namespace" not in d["metadata"] for d in visible)
    assert {d["metadata"]["namespace"] for d in app.users_documents()} == {"khaos"}
    images = {
        c["image"]
        for d in visible
        if d["kind"] in ("Deployment", "Job", "StatefulSet")
        for c in d["spec"]["template"]["spec"]["containers"]
    }
    assert "hidden:dev" not in images
