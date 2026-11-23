"""The tracker incident from end to end, without a cluster.

The hidden users (docker/apps/tracker/hidden/users.py) and the repairs
(replay.py, next to it) run against an in-memory stand-in for tracker-api, and
the oracle's judge grades what is left. One thread drives everything, so each
run is the same: users work between the steps of a repair, and where a test
asks for it a user changes a field in the instant before the repair writes it.

These are the problem's controls: the reference repair passes, and each
shortcut fails for its own reason and for no other.
"""

import argparse
import copy
import importlib.util
import json
import threading
import urllib.parse
import uuid
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from sregym.conductor.oracles.apps.tracker.restore_replay import judge, tracker_view

HIDDEN = Path(__file__).resolve().parents[2] / "docker/apps/tracker/hidden"
FIELDS = ("title", "description", "state", "priority", "assigneeId")
#: A small workspace: enough of every kind of lost work, and of everything users do when they are back.
TIER = {
    "hot": 12,
    "lost_creates": 3,
    "lost_comments": 10,
    "reentry": 5,
    "reposts": 2,
    "recreates": 0,
    "deletes": 2,
    "after_creates": 2,
    "after_comments": 2,
    "hot_share": 0.5,
}
WORKSPACES = 2


def load(name):
    spec = importlib.util.spec_from_file_location(f"tracker_hidden_{name}", HIDDEN / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeTracker:
    """tracker-api's routes and answers, over a dict. See docker/apps/tracker/cmd/api/main.go."""

    def __init__(self, workspaces, users=6, teams=2, issues=20, comments=1):
        self.lock = threading.RLock()
        self.users, self.teams, self.issues, self.comments = {}, {}, {}, {}
        self.log, self.listed, self.seq = [], [], 0
        self.saved = None
        for w in range(1, workspaces + 1):
            people = [str(uuid.uuid5(uuid.NAMESPACE_DNS, f"user-{w}-{u}")) for u in range(1, users + 1)]
            self.users.update({key: w for key in people})
            for t in range(1, teams + 1):
                team = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"team-{w}-{t}"))
                self.teams[team] = {"ws": w, "next": 1}
                for n in range(1, issues + 1):
                    body = {"id": str(uuid.uuid5(uuid.NAMESPACE_DNS, f"issue-{w}-{t}-{n}")), "teamId": team}
                    body.update(title=f"seed issue {n}", description="seeded.", state="todo", priority=n % 5)
                    issue = self.create_issue(people[n % users], w, body)[1]
                    for c in range(comments):
                        self.create_comment(people[(n + c) % users], issue, {"body": "seeded comment"})
        handler = type("Handler", (Handler,), {"tracker": self})
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    # ---- the database, for the test ----------------------------------------

    def backup(self):
        state = (self.teams, self.issues, self.comments, self.log, self.listed, self.seq)
        self.saved = copy.deepcopy(state)
        return self.seq

    def restore(self):
        """Back to the backup. Returns the lost window: the sync actions the backup does not have."""
        lost = [a for a in self.log if a["id"] > self.saved[-1]]
        self.teams, self.issues, self.comments, self.log, self.listed, self.seq = copy.deepcopy(self.saved)
        return lost

    def snapshot(self, after=None):
        with self.lock:
            return copy.deepcopy(
                {
                    "cursor": self.seq,
                    "issues": list(self.issues.values()),
                    "comments": list(self.comments.values()),
                    "listed": {(ws, action) for ws, action in self.listed},
                    "actions": [a for a in self.log if after is not None and a["id"] > after],
                }
            )

    # ---- the API ------------------------------------------------------------

    @staticmethod
    def now():
        return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")

    def record(self, ws, model, entity, action, delta, actor):
        self.log.append(
            {
                "id": self.seq,
                "workspaceId": ws,
                "model": model,
                "modelId": entity["id"],
                "action": action,
                "data": copy.deepcopy(entity),
                "delta": copy.deepcopy(delta),
                "actorId": actor,
                "createdAt": self.now(),
            }
        )

    def create_issue(self, user, ws, body):
        team = self.teams.get(body.get("teamId"))
        if team is None or team["ws"] != ws:
            return 400, {"error": "teamId is not a team of this workspace"}
        key = (body.get("id") or str(uuid.uuid4())).lower()
        if key in self.issues:
            return 409, {"error": "an issue with this id exists"}
        at = self.now()
        self.seq += 1
        issue = {"id": key, "workspaceId": ws, "teamId": body["teamId"], "number": team["next"]}
        issue.update(title=body["title"], description=body.get("description") or "")
        issue.update(state=body.get("state") or "todo", priority=body.get("priority") or 0)
        issue.update(assigneeId=body.get("assigneeId"), creatorId=user)
        issue.update(createdAt=body.get("createdAt") or at, updatedAt=at, syncId=self.seq)
        team["next"] += 1
        self.issues[key] = issue
        self.record(ws, "issue", issue, "I", None, user)
        return 201, issue

    def update_issue(self, user, issue, body, match):
        if match and int(match) != issue["syncId"]:
            return 412, {"error": "the issue has changed", "syncId": issue["syncId"]}
        if any(name not in FIELDS for name in body):
            return 400, {"error": "not a field of an issue"}
        delta = {name: value for name, value in body.items() if issue[name] != value}
        if delta:
            self.seq += 1
            issue.update(delta, updatedAt=self.now(), syncId=self.seq)
            self.record(issue["workspaceId"], "issue", issue, "U", delta, user)
        return 200, issue

    def delete_issue(self, user, issue):
        self.seq += 1
        del self.issues[issue["id"]]
        self.comments = {k: c for k, c in self.comments.items() if c["issueId"] != issue["id"]}
        self.record(issue["workspaceId"], "issue", issue, "D", None, user)
        return 200, {"deleted": issue["id"], "syncId": self.seq}

    def create_comment(self, user, issue, body):
        key = (body.get("id") or str(uuid.uuid4())).lower()
        if key in self.comments:
            return 409, {"error": "a comment with this id exists"}
        at = self.now()
        self.seq += 1
        comment = {"id": key, "workspaceId": issue["workspaceId"], "issueId": issue["id"], "userId": user}
        comment.update(body=body["body"], createdAt=body.get("createdAt") or at, updatedAt=at, syncId=self.seq)
        self.comments[key] = comment
        self.record(issue["workspaceId"], "comment", comment, "I", None, user)
        return 201, comment

    def answer(self, method, path, query, body, headers):
        parts = [p for p in path.split("/") if p]
        if parts[:1] == ["sync"]:
            ws = int(query["workspace"])
            if parts[1] == "bootstrap":
                last = max((a["id"] for a in self.log if a["workspaceId"] == ws), default=0)
                users = [{"id": key} for key, where in self.users.items() if where == ws]
                teams = [{"id": key} for key, team in self.teams.items() if team["ws"] == ws]
                issues = [i for i in self.issues.values() if i["workspaceId"] == ws]
                return 200, {"lastSyncId": last, "users": users, "teams": teams, "issues": issues}
            after, limit = int(query.get("after", 0)), int(query.get("limit", 500))
            return 200, {"actions": [a for a in self.log if a["workspaceId"] == ws and a["id"] > after][:limit]}
        if parts == ["admin", "recovery-items"] and method == "POST":
            self.listed.append((body["workspace"], body["action"]))
            return 201, body
        user = headers.get("X-User-Id")
        if user not in self.users:
            return (403, {"error": "unknown user"}) if user else (401, {"error": "X-User-Id is required"})
        ws = self.users[user]
        if parts[:1] == ["workspaces"] and parts[2:] == ["issues"] and method == "GET":
            if parts[1] != str(ws):
                return 403, {"error": "not your workspace"}
            found = [i for i in self.issues.values() if i["workspaceId"] == ws]
            return 200, {"issues": sorted(found, key=lambda i: (i["teamId"], i["number"]))}
        if parts == ["issues"] and method == "POST":
            return self.create_issue(user, ws, body)
        if parts[:1] == ["issues"] and len(parts) in (2, 3):
            issue = self.issues.get(parts[1])
            if issue is None or issue["workspaceId"] != ws:
                return 404, {"error": "issue not found"}
            if parts[2:] == ["comments"] and method == "POST":
                return self.create_comment(user, issue, body)
            if parts[2:] == ["comments"] and method == "GET":
                found = [c for c in self.comments.values() if c["issueId"] == issue["id"]]
                return 200, {"comments": sorted(found, key=lambda c: (c["createdAt"], c["id"]))}
            if len(parts) == 2 and method == "GET":
                return 200, issue
            if len(parts) == 2 and method == "PATCH":
                return self.update_issue(user, issue, body, (headers.get("If-Match") or "").strip('" '))
            if len(parts) == 2 and method == "DELETE":
                return self.delete_issue(user, issue)
        return 404, {"error": "no such route"}


class Handler(BaseHTTPRequestHandler):
    tracker = None

    def serve(self):
        url = urllib.parse.urlparse(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length)) if length else None
        with self.tracker.lock:
            query = dict(urllib.parse.parse_qsl(url.query))
            status, answer = self.tracker.answer(self.command, url.path, query, body, self.headers)
            data = json.dumps(answer).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_GET = do_POST = do_PATCH = do_DELETE = serve

    def log_message(self, *_):
        pass


class Incident:
    """Tracker after the restore, with its users back at work, at the moment the agent takes over."""

    def __init__(self, seed=2024, **tier):
        self.api = FakeTracker(WORKSPACES)
        self.users = load("users")
        self.users.ARGS = argparse.Namespace(api=self.api.url, seed=seed, **{**TIER, **tier})
        self.people = {number: self.users.Workspace(number) for number in range(1, WORKSPACES + 1)}
        self.backup = self.api.backup()
        for part in (1, 2):
            for ws in self.people.values():
                ws.run_lost(part)
        self.lost = self.api.restore()
        self.after = {}
        for number, ws in self.people.items():
            ws.bootstrap()
            self.after[number] = ws.run_after()
        self.work(3)
        self.handover = self.api.snapshot()

    def work(self, steps=1):
        """Steady work: each workspace's users do something, some of it on the issues that lost work."""
        for _ in range(steps):
            for ws in self.people.values():
                ws.follow()
                ws.live_step()

    def repair(self, mode, race_every=0, **options):
        """Put the lost window back with replay.py, users working in between.

        With race_every, before every so-manyth change the repair sends, a user changes the same field first.
        """
        replay = load("replay")
        options = {"stale_seconds": 0, "leave_same": False, "workers": 1, **options}
        replay.ARGS = argparse.Namespace(api=self.api.url, mode=mode, backup_sync_id=self.backup, **options)
        send, sent = replay.call, [0]

        def call(method, path, body=None, **more):
            if race_every and method == "PATCH":
                sent[0] += 1
                if sent[0] % race_every == 0:
                    key = path.rsplit("/", 1)[1]
                    ws = self.people[self.api.issues[key]["workspaceId"]]
                    ws.follow()
                    ws.update("live", key, [sorted(body)[0]])
            return send(method, path, body, **more)

        replay.call = call
        steps = {("issue", "I"): "issue_insert", ("issue", "U"): "issue_update", ("comment", "I"): "comment_insert"}
        workspaces = {}
        for action in self.lost:
            workspaces.setdefault(action["workspaceId"], replay.Workspace(action["workspaceId"], []))
        if mode == "stale":
            # The plan is made here, from the sync log as it is; users then work on before it is applied.
            for ws in workspaces.values():
                ws.follow()
            self.work(12)
        for count, action in enumerate(self.lost):
            getattr(workspaces[action["workspaceId"]], steps[(action["model"], action["action"])])(action)
            if count % 4 == 0:
                self.work()
        return sum((ws.stats for ws in workspaces.values()), start=replay.collections.Counter())

    def judge(self):
        self.work(3)  # users go on after the repair is handed in
        now = self.api.snapshot(after=self.handover["cursor"])
        return judge(
            tracker_view(self.handover),
            self.lost,
            list(self.users.RECORDS),
            tracker_view(now),
            now["listed"],
            now["actions"],
        )


@pytest.fixture
def incident():
    made = Incident()
    yield made
    made.api.close()


def reasons(found):
    return {reason: detail["count"] for reason, detail in found.items()}


def test_the_incident_has_every_kind_of_lost_work_and_users_posted_comments_again(incident):
    kinds = {(a["model"], a["action"]) for a in incident.lost}
    assert kinds == {("issue", "I"), ("issue", "U"), ("comment", "I")}
    assert incident.after == {n: {"retyped": 5, "deleted": 2, "reposted": 2} for n in (1, 2)}
    again = [w for w in incident.users.RECORDS if w.get("twin_of")]
    assert len(again) == 4 and all(w["status"] == "ok" and w["phase"] == "after" for w in again)
    lost = {a["modelId"]: a["data"] for a in incident.lost if a["model"] == "comment"}
    for w in again:
        # The same words by the same author on the same issue, as a new comment.
        was, now = lost[w["twin_of"]], w["entity"]
        assert (now["body"], now["userId"], now["issueId"]) == (was["body"], was["userId"], was["issueId"])
        assert now["id"] != was["id"] and now["createdAt"] > was["createdAt"]


def test_nothing_done_fails_for_what_is_missing(incident):
    found = reasons(incident.judge())
    assert set(found) == {"lost_work_missing", "skipped_not_listed"}


def test_the_reference_repair_passes_with_users_at_work(incident):
    stats = incident.repair("reference")
    assert stats["posted_again"] == 4 and stats["issues"] == 6
    assert incident.judge() == {}


def test_the_reference_repair_passes_when_users_change_fields_as_it_writes_them(incident):
    stats = incident.repair("reference", race_every=3)
    assert stats["retaken"] >= 5  # the step was taken again, with the user's change seen
    assert incident.judge() == {}


def test_without_the_precondition_a_change_made_in_between_is_overridden(incident):
    incident.repair("fresh", race_every=3)
    found = reasons(incident.judge())
    assert set(found) == {"user_changes_overridden"}


def test_a_plan_applied_later_overrides_what_users_did_meanwhile(incident):
    incident.repair("stale")
    found = reasons(incident.judge())
    assert set(found) == {"user_changes_overridden"}


def test_telling_what_is_there_by_id_alone_posts_comments_twice(incident):
    stats = incident.repair("keys")
    assert "posted_again" not in stats
    assert reasons(incident.judge()) == {"lost_work_duplicated": 4}


# ---- with lost issues created again by their creators (the tier key "recreates") ----


@pytest.fixture
def recreated():
    made = Incident(recreates=2)
    yield made
    made.api.close()


def test_an_issue_created_again_has_the_lost_issues_number_and_words(recreated):
    again = [w for w in recreated.users.RECORDS if w["kind"] == "issue_create" and w.get("twin_of")]
    assert len(again) == 4 and all(w["status"] == "ok" and w["phase"] == "after" for w in again)
    lost = {a["modelId"]: a["data"] for a in recreated.lost if (a["model"], a["action"]) == ("issue", "I")}
    for w in again:
        was, now = lost[w["twin_of"]], w["entity"]
        for name in ("teamId", "number", "title", "description", "creatorId", "state", "priority", "assigneeId"):
            assert now[name] == was[name], name
        assert now["id"] != was["id"]
        # Nothing else in the lost window touches the lost one.
        assert [a["action"] for a in recreated.lost if a["modelId"] == was["id"]] == ["I"]
        assert not [a for a in recreated.lost if a["model"] == "comment" and a["data"]["issueId"] == was["id"]]


def test_the_reference_repair_leaves_out_what_was_made_again(recreated):
    stats = recreated.repair("reference", race_every=3)
    assert stats["created_again"] == 4 and stats["posted_again"] == 4
    assert recreated.judge() == {}


def test_users_leave_an_issue_they_created_again_as_they_made_it(recreated):
    recreated.work(80)
    lost = {a["modelId"]: a["data"] for a in recreated.lost if (a["model"], a["action"]) == ("issue", "I")}
    again = [w for w in recreated.users.RECORDS if w["kind"] == "issue_create" and w.get("twin_of")]
    assert len(again) == 4
    for w in again:
        was, now = lost[w["twin_of"]], recreated.api.issues[w["id"]]
        assert (now["title"], now["description"]) == (was["title"], was["description"])
    # They do work on other issues made since the restore.
    made = {w["id"] for w in recreated.users.RECORDS if w["kind"] == "issue_create" and not w.get("twin_of")}
    assert any(w["kind"] == "issue_update" and w["id"] in made for w in recreated.users.RECORDS)


def test_a_repair_that_looks_at_the_issues_as_they_are_now_finds_them_after_a_while(recreated):
    recreated.work(80)  # some minutes of work before the repair begins
    stats = recreated.repair("rows")
    assert stats["created_again"] == 4 and stats["posted_again"] == 4
    assert recreated.judge() == {}


def test_telling_by_id_alone_makes_issues_and_comments_twice(recreated):
    recreated.repair("keys")
    assert reasons(recreated.judge()) == {"lost_work_duplicated": 8}


def test_matching_issues_alone_still_posts_comments_twice(recreated):
    stats = recreated.repair("issues")
    assert stats["created_again"] == 4 and "posted_again" not in stats
    found = recreated.judge()
    assert reasons(found) == {"lost_work_duplicated": 4}
    assert all("comment" in entry for entry in found["lost_work_duplicated"]["sample"])


def test_a_comment_left_out_because_it_was_posted_again_must_be_listed(incident):
    incident.repair("unlisted")
    assert reasons(incident.judge()) == {"skipped_not_listed": 4}


def test_a_change_the_field_already_holds_need_not_be_listed(incident):
    # Some users type the very value a lost change set; a repair that finds it there has nothing to list.
    stats = incident.repair("reference", leave_same=True)
    assert stats["same"] >= 1
    assert incident.judge() == {}


def test_replaying_everything_blindly_fails_on_every_count(incident):
    incident.repair("naive")
    found = reasons(incident.judge())
    assert {"user_changes_overridden", "lost_work_duplicated", "skipped_not_listed"} <= set(found)
