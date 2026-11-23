"""Grade putting lost work back into a live tracker by what its users are owed.

1. Users kept working: between the handover and the submission, no workspace
   went longer than MAX_GAP_SECONDS without one of its users' writes being
   accepted.
2. Hold: users go on working for HOLD_SECONDS after the submission, so a
   replay still running, or racing them, shows.
3. Then, with users paused, the tracker against what users are owed:
   - what users did since the restore stands: every field they changed holds
     their last change, what they created is there, what they deleted is gone;
   - the lost window is back: every issue and comment created in it, under its
     author and time, and every lost change to a field users have not changed
     since;
   - nothing else changed, and nothing is there twice: a lost comment (or
     issue) that its author had already made again when the agent took over
     stays as the author made it, and is not put back beside it;
   - what could not be put back when the agent took over (a lost change to a
     field users had already changed, or to an issue they had deleted, or a
     comment or issue its author had already made again) is listed on its
     workspace's recovery page. A lost change to a field is back, and need
     not be listed, if a user has since set the field to the very value it
     set;
   - a user's app, which holds a copy of its workspace and follows the sync
     log, ends with what the database holds;
   - tracker-api, which serves every read, serves what the database holds. It
     keeps issues in a cache, so rows changed behind it are served as they
     were, and a user who sets a field to the value it still shows is told
     the change is saved when nothing was written. A tracker-api started
     after the rows were changed holds no such copies. (The cache is in the
     process, which is why the application runs one tracker-api: a second
     one would not hear of what the first writes.)
"""

import copy
import re
import subprocess
import time
from collections import defaultdict
from datetime import UTC, datetime

from sregym.conductor.oracles.failure import FailureClass
from sregym.conductor.oracles.mitigation import MitigationOracle

MAX_GAP_SECONDS = 10
HOLD_SECONDS = 60

#: The fields of an issue a user can change, as the API names them, and as this module does.
API_FIELD = {
    "title": "title",
    "description": "description",
    "state": "state",
    "priority": "priority",
    "assigneeId": "assignee",
}
ISSUE_FIELDS = tuple(API_FIELD.values())
_TIME = re.compile(r"^(\d{4}-\d\d-\d\d)[T ](\d\d:\d\d:\d\d)(?:\.(\d+))?(Z|[+-]\d\d(?::?\d\d)?)?$")


def micros(value):
    """A timestamp, as the API or PostgreSQL writes it, in microseconds since the epoch."""
    if isinstance(value, (int, float)):
        return int(value)
    day, clock, fraction, zone = _TIME.match(value.strip()).groups()
    zone = "+00:00" if zone in (None, "Z") else zone
    if len(zone) == 3:
        zone += ":00"
    elif ":" not in zone:
        zone = zone[:3] + ":" + zone[3:]
    moment = datetime.fromisoformat(f"{day}T{clock}{zone}").astimezone(UTC)
    whole = int(moment.timestamp())
    return whole * 1_000_000 + int(((fraction or "") + "000000")[:6])


def issue_view(entity):
    """An issue as the API shows it, reduced to what users are owed."""
    return {
        "workspace": entity["workspaceId"],
        "team": entity["teamId"],
        "number": entity.get("number"),
        "title": entity["title"],
        "description": entity.get("description") or "",
        "state": entity["state"],
        "priority": entity["priority"],
        "assignee": entity.get("assigneeId"),
        "creator": entity["creatorId"],
        "created": micros(entity["createdAt"]),
    }


def comment_view(entity):
    return {
        "workspace": entity["workspaceId"],
        "issue": entity["issueId"],
        "user": entity["userId"],
        "body": entity["body"],
        "created": micros(entity["createdAt"]),
    }


def tracker_view(snapshot):
    """Issues and comments by id, from a snapshot of the database."""
    return {
        "issues": {e["id"]: issue_view(e) for e in snapshot["issues"]},
        "comments": {e["id"]: comment_view(e) for e in snapshot["comments"]},
    }


def write_gaps(writes, start, end, pauses=()):
    """Per workspace, the longest stretch between start and end in which none of its users' writes was accepted.

    pauses: (from, to) stretches in which the users were held still by an earlier grading; time inside
    them is not counted against anybody.
    """
    accepted = {}
    for w in writes:
        times = accepted.setdefault(w["ws"], [])  # a workspace whose every write failed still counts
        if w["status"] == "ok" and start <= w["ended"] <= end:
            times.append(w["ended"])

    def length(a, b):
        return b - a - sum(max(0.0, min(b, to) - max(a, since)) for since, to in pauses)

    worst = {}
    for ws, times in accepted.items():
        points = [start, *sorted(times), end]
        worst[ws] = max(length(a, b) for a, b in zip(points, points[1:], strict=False))
    return worst


def served_issues(app, tries=3, wait=5):
    """Every issue as tracker-api serves it to its users, by id. Nothing when it does not answer them.

    The users have just been asked for their record, so they are there; if they cannot read their
    issues it is tracker-api that is not serving, and that is what the grading should say.
    """
    for attempt in range(tries):
        try:
            return {entity["id"]: issue_view(entity) for entity in app.users_get("/served")["issues"]}
        except (subprocess.SubprocessError, ValueError, KeyError):
            if attempt + 1 < tries:
                time.sleep(wait)
    return {}


def client_copy(handover, log):
    """What a user's app holds: its copy at the handover, with every sync action since applied in order."""
    issues = dict(handover["issues"])
    comments = dict(handover["comments"])
    for action in sorted(log, key=lambda a: a["id"]):
        model, key, kind = action["model"], action["modelId"], action["action"]
        if model == "issue":
            if kind == "D":
                issues.pop(key, None)
                comments = {k: c for k, c in comments.items() if c["issue"] != key}
            else:
                issues[key] = issue_view(action["data"])
        elif model == "comment":
            if kind == "D":
                comments.pop(key, None)
            else:
                comments[key] = comment_view(action["data"])
    return {"issues": issues, "comments": comments}


def _acceptable(before, history):
    """The values a field may hold, given what it held before users touched it and their changes in order.

    A change counts once the API accepted it. One whose outcome the user never
    learned (a timeout, a broken connection) may or may not have been applied,
    so either reading is acceptable.
    """
    values = [before]
    for value, sure in history:
        values = [value] if sure else [*values, value]
    return values


def judge(handover, lost, writes, final, listed, log, served=None):
    """Compare the tracker with what its users are owed.

    handover, final: {"issues": {id: view}, "comments": {id: view}}, the
        database when the agent took over and now.
    lost: the lost window's sync actions, in order.
    writes: what users did since the restore, in order, as their apps recorded it.
        A comment or issue that repeats a lost one carries twin_of, the lost one's id.
    listed: {(workspace, lost action id)}, the recovery pages.
    log: the sync actions written since the handover.
    served: {id: view}, every issue as tracker-api serves it now; not checked when None.

    Returns {problem: details}, empty when everything is right.
    """
    found = defaultdict(list)

    # ---- what users did since the restore --------------------------------
    changed = defaultdict(list)  # (issue, field) -> [(value, sure)]
    changed_before = set()  # (issue, field) users had changed by the handover
    created_issues, created_comments = {}, {}  # id -> (view or None, sure)
    made_again = {}  # lost comment or issue -> (the one its author made again by the handover, sure)
    deleted, deleted_before = {}, set()  # issue -> sure
    for w in writes:
        if w["status"] == "failed":
            continue
        sure = w["status"] == "ok"
        before = w.get("phase") == "after"
        if w["kind"] == "issue_update":
            for name, value in w["fields"].items():
                changed[(w["id"], API_FIELD[name])].append((value, sure))
                if sure and before:
                    changed_before.add((w["id"], API_FIELD[name]))
        elif w["kind"] == "issue_create":
            created_issues[w["id"]] = (issue_view(w["entity"]) if sure else None, sure)
        elif w["kind"] == "comment_create":
            created_comments[w["id"]] = (comment_view(w["entity"]) if sure else None, sure)
        elif w["kind"] == "issue_delete":
            deleted[w["id"]] = sure or deleted.get(w["id"], False)
            if sure and before:
                deleted_before.add(w["id"])
        if before and w.get("twin_of"):
            made_again[w["twin_of"]] = (w["id"], sure)

    # ---- what the tracker is owed -----------------------------------------
    issues = {key: ("handover", dict(view)) for key, view in handover["issues"].items()}
    comments = {key: ("handover", dict(view)) for key, view in handover["comments"].items()}
    lost_fields = set()
    required = []  # lost actions that could not be put back when the agent took over
    twins = {}  # lost comment or issue -> (its lost action, the one its author made again)
    optional = set()  # what may or may not be there
    for action in lost:
        model, key, kind = action["model"], action["modelId"], action["action"]
        if kind == "I" and key in made_again:
            again, sure = made_again[key]
            if sure:
                # Its author has made it again. Putting it back too would show it twice.
                twins[key] = (action, again)
            else:
                # Nobody knows whether the one made again arrived, so nor whether this one is owed.
                optional.add(key)
        elif model == "issue" and kind == "I":
            issues[key] = ("lost", issue_view(action["data"]))
        elif model == "issue" and kind == "U":
            names = [n for n in (action.get("delta") or {}) if n in API_FIELD]
            # A field users had changed by the handover cannot take its lost value back. Unless a user
            # has typed that very value in: then the lost change is back, by their hand, and whoever
            # found the field holding it had nothing to put back and nothing to list.
            beyond = [
                n
                for n in names
                if (key, API_FIELD[n]) in changed_before
                and all(value != action["delta"][n] for value, _ in changed[(key, API_FIELD[n])])
            ]
            if key in deleted_before or beyond:
                required.append(action)
            if key in issues and issues[key][0] != "user":
                for name in names:
                    issues[key][1][API_FIELD[name]] = action["delta"][name]
                    lost_fields.add((key, API_FIELD[name]))
        elif model == "comment" and kind == "I":
            if action["data"]["issueId"] in deleted_before:
                required.append(action)
            comments[key] = ("lost", comment_view(action["data"]))
    for key, (view, sure) in created_issues.items():
        if sure:
            issues[key] = ("user", view)
        else:
            optional.add(key)
    for key, (view, sure) in created_comments.items():
        if sure:
            comments[key] = ("user", view)
        else:
            optional.add(key)

    def blame(origin):
        return {"user": "user_changes_overridden", "lost": "lost_work_missing"}.get(origin, "unexplained_changes")

    # ---- issues ------------------------------------------------------------
    for key, (origin, owed) in issues.items():
        got = final["issues"].get(key)
        if key in deleted:
            if deleted[key] and got is not None:
                found["user_changes_overridden"].append({"issue": key, "what": "users deleted it; it is back"})
            continue
        if got is None:
            found[blame(origin)].append({"issue": key, "what": "missing"})
            continue
        fixed = ("workspace", "team", "creator", "created") + (() if origin == "lost" else ("number",))
        for name in fixed:
            if got[name] != owed[name]:
                found[blame(origin)].append({"issue": key, "field": name, "owed": owed[name], "holds": got[name]})
        for name in ISSUE_FIELDS:
            history = changed.get((key, name), [])
            if got[name] in _acceptable(owed[name], history):
                continue
            if any(sure for _, sure in history):
                reason = "user_changes_overridden"
            elif origin == "lost" or (key, name) in lost_fields:
                reason = "lost_work_missing"
            else:
                reason = "unexplained_changes"
            owed_now = _acceptable(owed[name], history)[0]
            found[reason].append({"issue": key, "field": name, "owed": owed_now, "holds": got[name]})

    # ---- comments ----------------------------------------------------------
    for key, (origin, owed) in comments.items():
        got = final["comments"].get(key)
        if owed["issue"] in deleted or (owed["issue"] not in issues and owed["issue"] not in optional):
            # Its issue is gone (users deleted it), so the comment cannot be there either.
            continue
        if got is None:
            found[blame(origin)].append({"comment": key, "what": "missing"})
            continue
        for name in ("workspace", "issue", "user", "body", "created"):
            if got[name] != owed[name]:
                found[blame(origin)].append({"comment": key, "field": name, "owed": owed[name], "holds": got[name]})

    # ---- what its author had made again -------------------------------------
    for key, (action, again) in twins.items():
        kind = action["model"]
        if key not in final[kind + "s"]:
            if (action["workspaceId"], action["id"]) not in listed:
                found["skipped_not_listed"].append({"workspace": action["workspaceId"], "lost_action": action["id"]})
        elif again in final[kind + "s"]:
            found["lost_work_duplicated"].append({kind: key, "made_again_as": again})
        # Otherwise the author's own one is gone, which is found above as their work overridden.

    # ---- nothing else ------------------------------------------------------
    for kind, owed in (("issue", issues), ("comment", comments)):
        for key in final[kind + "s"]:
            if key in owed or key in optional or key in twins:
                continue
            if kind == "issue" and key in deleted:
                if deleted[key]:
                    found["user_changes_overridden"].append({"issue": key, "what": "users deleted it; it is back"})
                continue
            found["unexplained_changes"].append({kind: key, "what": "nobody created it"})

    # ---- recovery pages ----------------------------------------------------
    for action in required:
        if (action["workspaceId"], action["id"]) not in listed:
            found["skipped_not_listed"].append({"workspace": action["workspaceId"], "lost_action": action["id"]})

    # ---- users' apps -------------------------------------------------------
    app = client_copy(handover, log)
    for kind in ("issues", "comments"):
        for key in sorted(set(app[kind]) | set(final[kind])):
            if app[kind].get(key) != final[kind].get(key):
                found["clients_out_of_sync"].append({kind[:-1]: key})

    # ---- what tracker-api serves -------------------------------------------
    for key in sorted(set(served) | set(final["issues"])) if served is not None else ():
        has, holds = served.get(key), final["issues"].get(key)
        if has != holds:
            fields = sorted(name for name in has if has[name] != holds[name]) if has and holds else []
            what = fields or ("not served" if has is None else "served, but not in the database")
            found["api_serves_stale_data"].append({"issue": key, "differs": what})

    order = (
        "user_changes_overridden",
        "lost_work_missing",
        "lost_work_duplicated",
        "unexplained_changes",
        "skipped_not_listed",
        "clients_out_of_sync",
        "api_serves_stale_data",
    )
    return {reason: {"count": len(found[reason]), "sample": found[reason][:10]} for reason in order if found[reason]}


class RestoreReplayOracle(MitigationOracle):
    importance = 1.0
    evaluation_timeout_seconds = 900

    FAILURE_CLASSES = {
        "service_interrupted": FailureClass.AGENT_ERROR,
        "user_changes_overridden": FailureClass.AGENT_ERROR,
        "lost_work_missing": FailureClass.AGENT_ERROR,
        "lost_work_duplicated": FailureClass.AGENT_ERROR,
        "unexplained_changes": FailureClass.AGENT_ERROR,
        "skipped_not_listed": FailureClass.AGENT_ERROR,
        "clients_out_of_sync": FailureClass.AGENT_ERROR,
        "api_serves_stale_data": FailureClass.AGENT_ERROR,
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        #: Stretches of the users' clock in which a grading held the users still.
        self.pauses = []

    def evaluate(self) -> dict:
        print("== Tracker restore replay ==")
        app = self.problem.app
        try:
            return self._evaluate(app)
        except Exception as exc:  # noqa: BLE001
            print(f"[FAIL] grading error: {exc}")
            return self.fail_from_exception(exc)

    def _evaluate(self, app) -> dict:
        problem = self.problem
        submitted = app.users_clock()
        gaps = write_gaps(app.users_truth()["records"], problem.handover_at, submitted, self.pauses)
        worst = max(gaps.values(), default=0.0)
        print(f"longest time a workspace's users could not write: {worst:.1f}s (limit {MAX_GAP_SECONDS}s)")
        if worst > MAX_GAP_SECONDS:
            print("❌ users could not write")
            blocked = {ws: round(gap, 1) for ws, gap in gaps.items() if gap > MAX_GAP_SECONDS}
            return self.fail("service_interrupted", seconds=round(worst, 1), workspaces=blocked)

        result = super().evaluate()
        if not result.get("success"):
            return result

        print(f"hold: users keep working for {HOLD_SECONDS}s")
        time.sleep(HOLD_SECONDS)
        paused = app.users_clock()
        app.users_post("/phase?name=stop")
        try:
            time.sleep(5)
            writes = app.users_truth()["records"]
            now = app.snapshot(after=problem.handover["cursor"])
            served = served_issues(app)
        finally:
            # Users go back to work, so the tracker can be graded again (after the reference repair,
            # when the problem is validated); the stretch they were held still is not held against it.
            app.users_post("/phase?name=live")
            self.pauses.append((paused, app.users_clock()))
        found = judge(
            tracker_view(problem.handover),
            problem.lost,
            writes,
            tracker_view(now),
            {(ws, action) for ws, action in now["listed"]},
            now["actions"],
            served,
        )
        if found:
            first = next(iter(found))
            print(f"❌ the tracker is not what users are owed: {found}")
            return self.fail(first, **copy.deepcopy(found))
        print("✅ users kept working, their changes stand, and the lost window is back")
        return {"success": True, "lost_actions": len(problem.lost), "longest_write_gap_seconds": round(worst, 1)}
