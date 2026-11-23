#!/usr/bin/env python3
"""Tracker's users. Runs in the hidden namespace.

The users of every workspace, in the order the incident goes:

    lost   the work they do between the last backup and the restore
           (parts 1 and 2, either side of the migration that wipes comments)
    reset  Tracker comes back from the backup; their apps download it again
    after  what they do first: type some of their lost changes in again,
           post some of their lost comments again, delete a few issues, make
           new ones (and, with --recreates, create lost issues again)
    live   steady work from then on, some of it on the issues that lost work

Every write from "after" on is recorded with whether the API accepted it. The
conductor reads that record (127.0.0.1 only, with kubectl exec) as the truth
about what users did since the restore. A comment posted again, or an issue
created again, carries twin_of, the id of the lost one it repeats. The
conductor also asks for
every issue as tracker-api serves it to these users (/served).
"""

import argparse
import json
import math
import os
import random
import signal
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

FIELDS = ("title", "description", "state", "priority", "assigneeId")
STATES = ("backlog", "todo", "in_progress", "done", "canceled")
SUBJECTS = (
    "Login page times out on slow connections",
    "Export to CSV drops the last row",
    "Search ignores archived projects",
    "Typo in the onboarding email",
    "Webhook settings page does not save",
    "Dark mode contrast too low on tables",
    "Pagination resets after editing a filter",
    "Upload fails for files with spaces in the name",
    "Notification badge count is off by one",
    "Mobile layout overlaps the sidebar",
    "API returns 500 for empty labels",
    "Date picker shows the wrong week start",
)
NOTES = (
    "Reproduced on the latest release",
    "Still happening after clearing the cache",
    "Customer confirmed the workaround helps",
    "Seen again in the nightly run",
    "Attached logs from the affected tenant",
    "Narrowed down to the last deploy",
)

ARGS = None
RECORDS = []
RECORDS_LOCK = threading.Lock()
WORKSPACES = {}
LIVE = threading.Event()
RATE = [0.5]


def call(method, path, body=None, user=None, timeout=4.0):
    """One API call. Returns (status, body); status 0 when the outcome is not known, -1 when it never reached the API."""
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(ARGS.api + path, data=data, method=method)
    request.add_header("Content-Type", "application/json")
    if user:
        request.add_header("X-User-Id", user)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read() or b"null")
    except urllib.error.HTTPError as error:
        try:
            return error.code, json.loads(error.read() or b"null")
        except ValueError:
            return error.code, None
    except urllib.error.URLError as error:
        return (-1 if isinstance(error.reason, ConnectionRefusedError) else 0), None
    except (OSError, ValueError):
        return 0, None


def outcome(status):
    """ok: accepted. failed: certainly not applied. unknown: the user never learned."""
    if 200 <= status < 300:
        return "ok"
    if 400 <= status < 500 or status in (-1, 503):
        return "failed"
    return "unknown"


class Workspace:
    def __init__(self, number):
        self.number = number
        self.random = random.Random(ARGS.seed * 1000 + number)
        self.users, self.teams = [], []
        self.issues = {}  # this workspace as its users' apps hold it
        self.cursor = 0
        self.hot = []  # issues from before the backup that the lost window changed
        self.lost_pairs = set()  # (issue, field) the lost window changed
        self.lost_created = set()
        self.lost_commented = set()
        self.lost_posts = []  # the lost window's comments: id, issue, author, body
        self.lost_alone = []  # lost issues nothing else in the window touches: id, creator, what was sent
        self.remade = set()  # the issues created again
        self.script = []
        self.lock = threading.Lock()

    # ---- the app's copy ---------------------------------------------------

    def bootstrap(self):
        status, body = call("GET", f"/sync/bootstrap?workspace={self.number}", timeout=30)
        if status != 200:
            raise RuntimeError(f"bootstrap of workspace {self.number}: {status} {body}")
        self.users = [u["id"] for u in body["users"]]
        self.teams = [t["id"] for t in body["teams"]]
        self.issues = {i["id"]: i for i in body["issues"]}
        self.cursor = body["lastSyncId"]

    def follow(self):
        while True:
            status, body = call(
                "GET", f"/sync/delta?workspace={self.number}&after={self.cursor}&limit=1000", timeout=10
            )
            if status != 200:
                return
            for action in body["actions"]:
                self.cursor = action["id"]
                if action["model"] != "issue":
                    continue
                if action["action"] == "D":
                    self.issues.pop(action["modelId"], None)
                else:
                    self.issues[action["modelId"]] = action["data"]
            if len(body["actions"]) < 1000:
                return

    # ---- values -----------------------------------------------------------

    def token(self):
        return f"{self.random.getrandbits(24):06x}"

    def value(self, field, current=None):
        rng = self.random
        if field == "title":
            return f"{rng.choice(SUBJECTS)} ({self.token()})"
        if field == "description":
            return f"{rng.choice(NOTES)}. {rng.choice(NOTES)}. Ref {self.token()}."
        if field == "state":
            return rng.choice([s for s in STATES if s != current])
        if field == "priority":
            return rng.choice([p for p in range(5) if p != current])
        return rng.choice([u for u in [*self.users, None] if u != current])

    def new_issue(self):
        rng = self.random
        return {
            "id": str(uuid.UUID(int=rng.getrandbits(128), version=4)),
            "teamId": rng.choice(self.teams),
            "title": self.value("title"),
            "description": self.value("description"),
            "state": rng.choice(STATES[:3]),
            "priority": rng.randrange(5),
            "assigneeId": rng.choice([*self.users, None]),
        }

    def new_comment(self):
        return {
            "id": str(uuid.UUID(int=self.random.getrandbits(128), version=4)),
            "body": f"{self.random.choice(NOTES)} ({self.token()})",
        }

    # ---- writes -----------------------------------------------------------

    def write(self, phase, kind, key, method, path, body, user=None, **more):
        user = user or self.random.choice(self.users)
        started = time.time()
        status, answer = call(method, path, body, user)
        result = outcome(status)
        if phase != "lost":
            entry = {
                "ws": self.number,
                "kind": kind,
                "id": key,
                "status": result,
                "http": status,
                "phase": phase,
                "started": started,
                "ended": time.time(),
                **more,
            }
            if kind == "issue_update":
                entry["fields"] = body
            elif kind in ("issue_create", "comment_create"):
                entry["entity"] = answer if result == "ok" else body
            with RECORDS_LOCK:
                entry["seq"] = len(RECORDS) + 1
                RECORDS.append(entry)
        if result == "ok":
            if kind in ("issue_create", "issue_update"):
                self.issues[key] = answer
            elif kind == "issue_delete":
                self.issues.pop(key, None)
        elif phase == "lost":
            raise RuntimeError(f"workspace {self.number}: {method} {path} -> {status} {answer}")
        return result

    def update(self, phase, key, fields):
        body = {f: self.value(f, self.issues.get(key, {}).get(f)) for f in fields}
        return self.write(phase, "issue_update", key, "PATCH", f"/issues/{key}", body)

    def create(self, phase):
        body = self.new_issue()
        self.write(phase, "issue_create", body["id"], "POST", "/issues", body)
        return body["id"]

    def create_alone(self):
        """A lost issue that nothing else in the lost window touches, remembered so its creator can make it again."""
        body = self.new_issue()
        user = self.random.choice(self.users)
        self.write("lost", "issue_create", body["id"], "POST", "/issues", body, user=user)
        self.lost_alone.append({"id": body["id"], "user": user, "body": body})

    def recreate(self, phase, lost):
        """A lost issue's creator makes it again: the same team, title and text, as a new issue."""
        body = {**lost["body"], "id": str(uuid.UUID(int=self.random.getrandbits(128), version=4))}
        more = {"user": lost["user"], "twin_of": lost["id"]}
        self.remade.add(body["id"])
        return self.write(phase, "issue_create", body["id"], "POST", "/issues", body, **more)

    def comment(self, phase, key):
        body = self.new_comment()
        user = self.random.choice(self.users)
        if phase == "lost":
            self.lost_posts.append({"id": body["id"], "issue": key, "user": user, "body": body["body"]})
        path = f"/issues/{key}/comments"
        return self.write(phase, "comment_create", body["id"], "POST", path, body, user=user, issue=key)

    def repost(self, phase, post):
        """A lost comment's author posts it again: the same words on the same issue, as a new comment."""
        body = {"id": str(uuid.UUID(int=self.random.getrandbits(128), version=4)), "body": post["body"]}
        path, more = f"/issues/{post['issue']}/comments", {"issue": post["issue"], "twin_of": post["id"]}
        return self.write(phase, "comment_create", body["id"], "POST", path, body, user=post["user"], **more)

    def delete(self, phase, key):
        return self.write(phase, "issue_delete", key, "DELETE", f"/issues/{key}", None)

    # ---- the lost window ----------------------------------------------------

    def plan_lost(self):
        """The lost window's work: changes and comments on issues from before the backup, and new issues."""
        rng = self.random
        self.bootstrap()
        self.hot = rng.sample(sorted(self.issues), min(ARGS.hot, len(self.issues)))
        steps = []
        for key in self.hot:
            first = rng.sample(FIELDS, 2)
            second = [rng.choice([f for f in FIELDS if f not in first])]
            steps += [("update", key, first), ("update", key, second)]
            self.lost_pairs.update((key, f) for f in first + second)
        for key in rng.choices(self.hot, k=ARGS.lost_comments):
            steps.append(("comment", key, None))
            self.lost_commented.add(key)
        rng.shuffle(steps)
        for _ in range(ARGS.lost_creates):
            # A new issue, then a change to it and a comment on it, somewhere later.
            slot = rng.randrange(len(steps) + 1)
            tag = object()
            steps.insert(slot, ("create", tag, None))
            for kind in ("update", "comment"):
                steps.insert(rng.randrange(slot + 1, len(steps) + 1), (kind, tag, [rng.choice(FIELDS)]))
        # First in the window, so each is the first new issue of its team and gets the first new number.
        self.script = [("alone", None, None)] * ARGS.recreates + steps

    def run_lost(self, part):
        if part == 1:
            self.plan_lost()
            self.tags = {}
            steps, self.script = (
                self.script[: math.ceil(len(self.script) * 0.6)],
                self.script[math.ceil(len(self.script) * 0.6) :],
            )
        else:
            steps, self.script = self.script, []
        for kind, key, fields in steps:
            if kind == "alone":
                self.create_alone()
                continue
            if kind == "create":
                self.tags[key] = self.create("lost")
                self.lost_created.add(self.tags[key])
                continue
            key = self.tags.get(key, key)
            if kind == "update":
                self.update("lost", key, fields)
                self.lost_pairs.update((key, f) for f in fields)
            else:
                self.comment("lost", key)
        return len(steps)

    # ---- back from the backup ------------------------------------------------

    def run_after(self):
        """What users do first when Tracker is back.

        They retype some lost changes, delete a few issues, make new ones, and some post a lost comment
        of theirs again.
        """
        rng = self.random
        there = [key for key in self.hot if key in self.issues]
        rng.shuffle(there)
        commented = [key for key in there if key in self.lost_commented]
        doomed = (commented + [key for key in there if key not in commented])[: ARGS.deletes]
        retyped = [key for key in there if key not in doomed][: ARGS.reentry]
        for key in retyped:
            field = rng.choice(sorted(f for k, f in self.lost_pairs if k == key))
            self.update("after", key, [field])
        for key in doomed:
            self.delete("after", key)
        # Before anybody makes a new issue, and in the order they were first made: the team's counter went
        # back with the restore, so each issue made again gets the number the lost one had.
        recreated = sum(self.recreate("after", lost) == "ok" for lost in self.lost_alone)
        for _ in range(ARGS.after_creates):
            self.create("after")
        # A lost comment can be posted again where its issue is still there: one from before the backup
        # that nobody has deleted.
        again = [post for post in self.lost_posts if post["issue"] in self.issues]
        posts = [None] * ARGS.after_comments + rng.sample(again, min(ARGS.reposts, len(again)))
        rng.shuffle(posts)
        reposted = 0
        for post in posts:
            if post is None:
                self.comment("after", rng.choice(sorted(self.issues)))
            else:
                reposted += self.repost("after", post) == "ok"
        done = {"retyped": len(retyped), "deleted": len(doomed), "reposted": reposted}
        return {**done, "recreated": recreated} if ARGS.recreates else done

    # ---- steady work -----------------------------------------------------------

    def live_step(self):
        rng = self.random
        there = sorted(self.issues)
        if not there:
            return
        lost = self.lost_created | set(self.hot)
        hot = [key for key in there if key in lost]
        # An issue just created again is left as its creator made it: if its title or text moved on, it
        # would no longer say what the lost one says, and whether the two are one issue would be a guess.
        cold = [key for key in there if key not in lost and key not in self.remade]
        roll = rng.random()
        if roll < ARGS.hot_share and hot:
            # Back on an issue that lost work, most often on the very field that lost it.
            pairs = sorted((k, f) for k, f in self.lost_pairs if k in self.issues)
            if pairs and rng.random() < 0.7:
                key, field = rng.choice(pairs)
            else:
                key, field = rng.choice(hot), rng.choice(FIELDS)
            self.update("live", key, [field])
        elif roll < ARGS.hot_share + 0.45 and cold:
            self.update("live", rng.choice(cold), [rng.choice(FIELDS)])
        elif roll < ARGS.hot_share + 0.75:
            self.comment("live", rng.choice(there))
        else:
            self.create("live")

    def live(self):
        followed = 0.0
        while True:
            LIVE.wait()
            rate = RATE[0]
            if rate <= 0:
                time.sleep(0.5)
                continue
            time.sleep(self.random.uniform(0.7, 1.3) / rate)
            if not LIVE.is_set():
                continue
            with self.lock:
                if time.time() - followed > 3:
                    self.follow()
                    followed = time.time()
                try:
                    self.live_step()
                except Exception as error:  # noqa: BLE001 - a user carries on
                    print(f"workspace {self.number}: {error}", flush=True)


def phase(name, part):
    if name == "live":
        LIVE.set()
        return {"live": True}
    if name == "stop":
        LIVE.clear()
        for ws in WORKSPACES.values():
            with ws.lock:  # let the write in hand finish
                pass
        return {"live": False}
    done = {}
    for ws in WORKSPACES.values():
        with ws.lock:
            if name == "lost":
                done[ws.number] = ws.run_lost(part)
            elif name == "reset":
                ws.bootstrap()
                done[ws.number] = len(ws.issues)
            elif name == "after":
                done[ws.number] = ws.run_after()
            else:
                raise ValueError(f"no phase named {name}")
    return {"phase": name, "workspaces": done}


class Status(BaseHTTPRequestHandler):
    def answer(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        if url.path == "/clock":
            return self.answer(200, {"now": time.time()})
        if url.path == "/truth":
            with RECORDS_LOCK:
                records = list(RECORDS)
            return self.answer(200, {"now": time.time(), "records": records})
        if url.path == "/served":
            issues = []
            for ws in WORKSPACES.values():
                status, body = call("GET", f"/workspaces/{ws.number}/issues", user=ws.users[0], timeout=60)
                if status != 200:
                    return self.answer(502, {"error": f"workspace {ws.number}: {status} {body}"})
                issues += body["issues"]
            return self.answer(200, {"issues": issues})
        if url.path == "/status":
            with RECORDS_LOCK:
                counts = {}
                for r in RECORDS:
                    key = f"{r['phase']}:{r['status']}"
                    counts[key] = counts.get(key, 0) + 1
            return self.answer(200, {"now": time.time(), "live": LIVE.is_set(), "rate": RATE[0], "writes": counts})
        return self.answer(404, {"error": "not found"})

    def do_POST(self):
        url = urllib.parse.urlparse(self.path)
        query = dict(urllib.parse.parse_qsl(url.query))
        try:
            if url.path == "/phase":
                return self.answer(200, phase(query["name"], int(query.get("part", 1))))
            if url.path == "/rate":
                RATE[0] = float(query["per_workspace"])
                return self.answer(200, {"rate": RATE[0]})
        except Exception as error:  # noqa: BLE001 - the conductor reads the reason
            return self.answer(500, {"error": f"{type(error).__name__}: {error}"})
        return self.answer(404, {"error": "not found"})

    def log_message(self, *_):
        pass


def main():
    global ARGS
    parser = argparse.ArgumentParser()
    parser.add_argument("--api", required=True)
    parser.add_argument("--workspaces", type=int, required=True)
    parser.add_argument("--seed", type=int, default=2024)
    parser.add_argument("--hot", type=int, default=40, help="issues per workspace the lost window changes")
    parser.add_argument("--lost-creates", type=int, default=6)
    parser.add_argument("--lost-comments", type=int, default=24)
    parser.add_argument("--reentry", type=int, default=8, help="lost changes users type in again before the handover")
    parser.add_argument("--reposts", type=int, default=3, help="lost comments posted again before the handover")
    parser.add_argument("--recreates", type=int, default=0, help="lost issues created again before the handover")
    parser.add_argument("--deletes", type=int, default=3)
    parser.add_argument("--after-creates", type=int, default=4)
    parser.add_argument("--after-comments", type=int, default=4)
    parser.add_argument("--rate", type=float, default=0.5, help="writes per second per workspace")
    parser.add_argument("--hot-share", type=float, default=0.15, help="share of steady writes on issues that lost work")
    parser.add_argument("--port", type=int, default=8091)
    ARGS = parser.parse_args()
    RATE[0] = ARGS.rate
    # As the container's first process a SIGTERM would otherwise be ignored, and these users would go on
    # writing, into the next deployment of Tracker, until the grace period ran out.
    signal.signal(signal.SIGTERM, lambda *_: os._exit(0))
    for number in range(1, ARGS.workspaces + 1):
        WORKSPACES[number] = Workspace(number)
        threading.Thread(target=WORKSPACES[number].live, daemon=True).start()
    print(f"users of {ARGS.workspaces} workspaces, against {ARGS.api}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", ARGS.port), Status).serve_forever()


if __name__ == "__main__":
    main()
