#!/usr/bin/env python3
"""Put the lost window back through tracker-api. Runs in the hidden namespace.

The reference repair, and the shortcuts the control scenarios price. The lost
sync actions arrive as JSON on stdin, in order.

    reference  each lost action in turn, as the user who made it, with its id
               and time. Before changing an issue: read it, read every sync
               action up to its syncId, leave the fields users have changed
               since the restore, and send the rest with If-Match, so a change
               a user makes in between is seen and the step is taken again.
               Before posting a lost comment: read the issue's comments, and
               leave it out if its author has posted the same words again.
               Before creating a lost issue: leave it out if its creator has
               created the same issue again since the restore.
    keys       the same, but telling what is already there by id alone, so
               what its author made again is made a second time.
    issues     the same, but only issues are compared by what they say, not
               comments.
    rows       the reference, but an issue created again is looked for among
               the workspace's issues as they are now, not in the sync log.
    unlisted   the reference, but what is left out because its author made it
               again is not listed.
    fresh      the reference, without If-Match.
    stale      decide once from the sync log as it is now, wait
               --stale-seconds, then apply in parallel without looking again.
    naive      apply every lost action, never looking at what users did.

What is not put back is listed on the workspace's recovery page. With
--leave-same, a lost change to a field users have changed since is not listed
when the field already holds the value it set.
"""

import argparse
import collections
import concurrent.futures
import json
import sys
import threading
import time
import urllib.error
import urllib.request

ARGS = None


def call(method, path, body=None, user=None, headers=None, timeout=15.0):
    data = json.dumps(body).encode() if body is not None else None
    last = None
    for _ in range(4):
        request = urllib.request.Request(ARGS.api + path, data=data, method=method)
        request.add_header("Content-Type", "application/json")
        if user:
            request.add_header("X-User-Id", user)
        for name, value in (headers or {}).items():
            request.add_header(name, value)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.status, json.loads(response.read() or b"null")
        except urllib.error.HTTPError as error:
            status, answer = error.code, error.read()
            if status < 500:
                return status, json.loads(answer or b"null")
            last = f"{status} {answer[:200]!r}"
        except (urllib.error.URLError, OSError) as error:
            last = str(error)
        time.sleep(1)
    raise RuntimeError(f"{method} {path}: {last}")


class Workspace:
    """One workspace's lost actions, and what its users have done since the restore."""

    def __init__(self, number, actions):
        self.number = number
        self.actions = actions
        self.cursor = ARGS.backup_sync_id
        self.mine = set()  # sync ids of the actions this replay wrote
        self.changed = collections.defaultdict(set)  # issue -> fields users changed since the restore
        self.made = {}  # (team, creator, title, text) -> the issue a user created since the restore
        self.stats = collections.Counter()

    def follow(self, upto=0):
        """Read the workspace's sync log up to now (and at least up to a sync id)."""
        while True:
            status, body = call("GET", f"/sync/delta?workspace={self.number}&after={self.cursor}&limit=2000")
            if status != 200:
                raise RuntimeError(f"delta: {status} {body}")
            for action in body["actions"]:
                self.cursor = action["id"]
                if action["id"] in self.mine or action["model"] != "issue":
                    continue
                if action["action"] == "U":
                    self.changed[action["modelId"]].update(action.get("delta") or {})
                elif action["action"] == "I":
                    self.made[self.says(action["data"])] = action["modelId"]
            if len(body["actions"]) < 2000 and self.cursor >= upto:
                return
            if not body["actions"]:
                time.sleep(0.05)

    def list(self, action, note):
        status, body = call(
            "POST", "/admin/recovery-items", {"workspace": self.number, "action": action["id"], "note": note}
        )
        if status != 201:
            raise RuntimeError(f"recovery page: {status} {body}")
        self.stats["listed"] += 1

    def wrote(self, answer, before=None):
        if answer["syncId"] != before:
            self.mine.add(answer["syncId"])

    # ---- one lost action ----------------------------------------------------

    @staticmethod
    def says(issue):
        return issue["teamId"], issue["creatorId"], issue["title"], issue["description"]

    def created_again(self, action):
        """Whether the issue's creator has created the same issue again, as another issue."""
        data = action["data"]
        if ARGS.mode == "rows":
            status, body = call("GET", f"/workspaces/{self.number}/issues", user=action["actorId"])
            if status != 200:
                raise RuntimeError(f"issues of workspace {self.number}: {status} {body}")
            return any(i["id"] != data["id"] and self.says(i) == self.says(data) for i in body["issues"])
        if ARGS.mode != "stale":  # a plan made earlier stays as it was made
            self.follow()
        return self.made.get(self.says(data), data["id"]) != data["id"]

    def issue_insert(self, action):
        data = action["data"]
        if ARGS.mode not in ("keys", "naive") and self.created_again(action):
            self.stats["created_again"] += 1
            if ARGS.mode != "unlisted":
                self.list(action, "This issue was not restored: its creator has created it again since.")
            return
        body = {
            k: data[k] for k in ("id", "teamId", "title", "description", "state", "priority", "assigneeId", "createdAt")
        }
        status, answer = call("POST", "/issues", body, user=action["actorId"])
        if status == 201:
            self.wrote(answer)
            self.stats["issues"] += 1
        elif status != 409:  # 409: already there, from an earlier run
            raise RuntimeError(f"issue {data['id']}: {status} {answer}")

    def posted_again(self, action):
        """Whether the comment's author has posted the same words on the issue again, as another comment."""
        data = action["data"]
        status, answer = call("GET", f"/issues/{data['issueId']}/comments", user=action["actorId"])
        if status != 200:  # the POST that follows says why
            return False
        return any(
            c["id"] != data["id"] and c["userId"] == data["userId"] and c["body"] == data["body"]
            for c in answer["comments"]
        )

    def comment_insert(self, action):
        data = action["data"]
        if ARGS.mode not in ("keys", "issues", "naive") and self.posted_again(action):
            self.stats["posted_again"] += 1
            if ARGS.mode != "unlisted":
                self.list(action, "This comment was not restored: its author has posted it again since.")
            return
        body = {"id": data["id"], "body": data["body"], "createdAt": data["createdAt"]}
        status, answer = call("POST", f"/issues/{data['issueId']}/comments", body, user=action["actorId"])
        if status == 201:
            self.wrote(answer)
            self.stats["comments"] += 1
        elif status == 404:
            self.list(action, "This comment could not be restored: its issue has been deleted since.")
        elif status != 409:
            raise RuntimeError(f"comment {data['id']}: {status} {answer}")

    def issue_update(self, action):
        key, fields = action["modelId"], dict(action["delta"])
        user = action["actorId"]
        if ARGS.mode in ("stale", "naive"):
            kept = set(fields) & self.changed[key] if ARGS.mode == "stale" else set()
            send = {k: v for k, v in fields.items() if k not in kept}
            status = 200
            if send:
                status, answer = call("PATCH", f"/issues/{key}", send, user=user)
            if status == 404:
                return self.list(action, "This change could not be restored: the issue has been deleted since.")
            if status != 200:
                raise RuntimeError(f"issue {key}: {status} {answer}")
            self.stats["changes"] += bool(send)
            if kept:
                self.list(action, f"Not restored, because it has been changed since: {', '.join(sorted(kept))}.")
            return None
        for _ in range(20):
            status, issue = call("GET", f"/issues/{key}", user=user)
            if status == 404:
                return self.list(action, "This change could not be restored: the issue has been deleted since.")
            if status != 200:
                raise RuntimeError(f"issue {key}: {status} {issue}")
            self.follow(upto=issue["syncId"])
            kept = set(fields) & self.changed[key]
            send = {k: v for k, v in fields.items() if k not in kept}
            if send:
                headers = {"If-Match": str(issue["syncId"])} if ARGS.mode != "fresh" else None
                status, answer = call("PATCH", f"/issues/{key}", send, user=user, headers=headers)
                if status == 412:
                    self.stats["retaken"] += 1
                    continue
                if status == 404:
                    return self.list(action, "This change could not be restored: the issue has been deleted since.")
                if status != 200:
                    raise RuntimeError(f"issue {key}: {status} {answer}")
                self.wrote(answer, before=issue["syncId"])
                self.stats["changes"] += 1
            if ARGS.leave_same:
                self.stats["same"] += sum(issue[k] == fields[k] for k in kept)
                kept = {k for k in kept if issue[k] != fields[k]}
            if kept:
                self.list(action, f"Not restored, because it has been changed since: {', '.join(sorted(kept))}.")
            return None
        raise RuntimeError(f"issue {key} kept changing")

    def run(self):
        for action in self.actions:
            kind = (action["model"], action["action"])
            if kind == ("issue", "I"):
                self.issue_insert(action)
            elif kind == ("issue", "U"):
                self.issue_update(action)
            elif kind == ("comment", "I"):
                self.comment_insert(action)
            else:
                raise RuntimeError(f"no way to put back {kind}")
        return self.stats


def main():
    global ARGS
    parser = argparse.ArgumentParser()
    parser.add_argument("--api", required=True)
    modes = ("reference", "keys", "issues", "rows", "unlisted", "fresh", "stale", "naive")
    parser.add_argument("--mode", choices=modes, default="reference")
    parser.add_argument("--backup-sync-id", type=int, required=True, help="the last sync id in the backup")
    parser.add_argument("--stale-seconds", type=float, default=60)
    parser.add_argument("--leave-same", action="store_true", help="do not list a change the field already holds")
    parser.add_argument("--workers", type=int, default=8)
    ARGS = parser.parse_args()

    by_workspace = collections.defaultdict(list)
    for action in json.load(sys.stdin):
        by_workspace[action["workspaceId"]].append(action)
    workspaces = [Workspace(number, actions) for number, actions in sorted(by_workspace.items())]
    started = time.time()
    if ARGS.mode == "stale":
        for ws in workspaces:
            ws.follow()
        time.sleep(ARGS.stale_seconds)
        with concurrent.futures.ThreadPoolExecutor(ARGS.workers) as pool:
            list(pool.map(Workspace.run, workspaces))  # raises what a workspace raised
    else:
        for ws in workspaces:
            ws.run()
    total = collections.Counter()
    for ws in workspaces:
        total.update(ws.stats)
    print(json.dumps({"mode": ARGS.mode, "seconds": round(time.time() - started, 1), **total}))


if __name__ == "__main__":
    threading.stack_size(1 << 20)
    main()
