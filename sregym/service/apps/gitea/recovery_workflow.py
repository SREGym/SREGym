"""Issue journal operations, also executed with the real API inside the client pod."""

import base64
import http.cookiejar
import json
import os
import urllib.parse
import urllib.request
from pathlib import Path


def create_issues(api, records):
    receipts = []
    for record in records:
        issue = api(
            f"/repos/{record['repository']}/issues",
            "POST",
            {"title": record["title"], "body": record["body"]},
        )
        receipts.append({**record, "number": issue["number"]})
    return receipts


def reconcile_issues(api, receipts, write=False):
    """Replay only absent records, preserving public issue numbers and content.

    A conflicting issue is an integrity error, not permission to overwrite it.
    Receipt order preserves the per-repository allocation order of issue numbers.
    """
    created = 0
    for receipt in receipts:
        path = f"/repos/{receipt['repository']}/issues"
        issue = api(f"{path}/{receipt['number']}", missing_ok=True)
        if issue is None and write:
            issue = api(path, "POST", {"title": receipt["title"], "body": receipt["body"]})
            created += 1
        if issue is None or any(issue.get(key) != receipt[key] for key in ("number", "title", "body")):
            raise RuntimeError(f"Issue receipt not reconciled: {receipt['repository']}#{receipt['number']}")
    return {"verified": len(receipts), "created": created}


def create_webhooks(api, repositories, url, secret):
    created = 0
    for repository in repositories:
        if any(hook["config"]["url"] == url for hook in api(f"/repos/{repository}/hooks")):
            continue
        api(
            f"/repos/{repository}/hooks",
            "POST",
            {
                "type": "gitea",
                "active": True,
                "events": ["issues_only", "issue_comment"],
                "config": {"url": url, "content_type": "json", "secret": secret},
            },
        )
        created += 1
    return {"created": created}


NAMED = {}


def contents(repository, name):
    return f"/repos/{repository}/contents/" + urllib.parse.quote(name, safe="/")


def put_file(api, repository, name, content):
    old = api(contents(repository, name), missing_ok=True)
    data = {"content": base64.b64encode(content.encode()).decode(), "message": "Update " + name}
    if old:
        data["sha"] = old["sha"]
    api(contents(repository, name), "PUT" if old else "POST", data)


def named(api, repository, kind, name):
    """The id of a repository's label or milestone by name."""
    if (repository, kind) not in NAMED:
        items = api(f"/repos/{repository}/{kind}?state=all&limit=50")
        NAMED[repository, kind] = {item.get("name") or item.get("title"): item["id"] for item in items}
    if name not in NAMED[repository, kind]:
        raise RuntimeError(f"No {kind[:-1]} {name!r} in {repository}")
    return NAMED[repository, kind][name]


def update_issues(api, updates):
    """Comment on, close, label, assign, milestone or react to existing issues; comment receipts carry the id."""
    receipts = []
    for update in updates:
        path = f"/repos/{update['repository']}/issues/{update['number']}"
        if "comment" in update:
            comment = api(path + "/comments", "POST", {"body": update["comment"]})
            receipts.append({**update, "comment_id": comment["id"]})
            continue
        if update.get("close"):
            api(path, "PATCH", {"state": "closed"})
        elif "label" in update:
            api(path + "/labels", "POST", {"labels": [named(api, update["repository"], "labels", update["label"])]})
        elif "assignee" in update:
            assignees = [user["login"] for user in api(path)["assignees"] or []]
            api(path, "PATCH", {"assignees": sorted({*assignees, update["assignee"]})})
        elif "milestone" in update:
            api(path, "PATCH", {"milestone": named(api, update["repository"], "milestones", update["milestone"])})
        elif "reaction" in update:
            api(path + "/reactions", "POST", {"content": update["reaction"]})
        else:
            raise ValueError(update)
        receipts.append(update)
    return receipts


def create_catalog(api, repositories, labels, milestones):
    """Labels and milestones each repository's issues are triaged with."""
    for repository in repositories:
        for index, name in enumerate(labels):
            api(
                f"/repos/{repository}/labels",
                "POST",
                {"name": name, "color": f"#{(index + 1) * 0x2A3B4C % 0xFFFFFF:06x}"},
            )
        for title in milestones:
            api(f"/repos/{repository}/milestones", "POST", {"title": title})
    return {"repositories": len(repositories)}


def create_repository(api, owner, name, files):
    api(f"/orgs/{owner}/repos", "POST", {"name": name, "auto_init": True, "default_branch": "main"})
    for path, content in files.items():
        put_file(api, f"{owner}/{name}", path, content)
    return {"repository": f"{owner}/{name}"}


def push_files(api, pushes):
    for push in pushes:
        put_file(api, push["repository"], push["path"], push["content"])
    return {"pushed": len(pushes)}


def read_files(api, files):
    """Each file's content as Gitea serves it, or None when Gitea does not serve it."""
    served = []
    for item in files:
        found = api(contents(item["repository"], item["path"]), missing_ok=True)
        served.append(base64.b64decode(found["content"]).decode() if found else None)
    return served


def adopt_repositories(api, repositories):
    for repository in repositories:
        api(f"/admin/unadopted/{repository}", "POST")
    return {"adopted": len(repositories)}


def search_issues(api, queries):
    """Issue numbers Gitea's search index returns for each query."""
    found = []
    for query in queries:
        path = f"/repos/{query['repository']}/issues?state=all&type=issues&q=" + urllib.parse.quote(query["text"])
        found.append(sorted(issue["number"] for issue in api(path)))
    return found


def add_member(api, username, password, repository):
    """A non-admin user with write access to one repository."""
    if api(f"/users/{username}", missing_ok=True) is None:
        api(
            "/admin/users",
            "POST",
            {
                "username": username,
                "email": f"{username}@example.com",
                "password": password,
                "must_change_password": False,
            },
        )
    else:
        api(
            f"/admin/users/{username}",
            "PATCH",
            {"login_name": username, "source_id": 0, "password": password, "must_change_password": False},
        )
    api(f"/repos/{repository}/collaborators/{username}", "PUT", {"permission": "write"})
    return {"username": username}


def admin_session():
    """A signed-in browser session for admin actions the API does not offer."""
    base = os.environ.get("GITEA_URL", "http://gitea:3000")
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    credentials = {
        "user_name": Path("/credentials/username").read_text().strip(),
        "password": Path("/credentials/password").read_text().strip(),
    }

    def post(path, form):
        with opener.open(base + path, urllib.parse.urlencode(form).encode(), timeout=60) as response:
            if "/user/login" in response.geturl():
                raise RuntimeError(f"Gitea refused the admin session at {path}")

    post("/user/login", credentials)
    return post


def sync_branches():
    """Run the admin dashboard's "Sync missed branches from git data to databases"; Gitea queues it."""
    admin_session()("/-/admin", {"op": "sync_repo_branches"})
    return {"queued": True}


def replay_webhooks(tasks):
    """Send stored webhook deliveries again through each hook's Replay action."""
    post = admin_session()
    for task in tasks:
        post(f"/{task['repository']}/settings/hooks/{task['hook_id']}/replay/{task['uuid']}", {})
    return {"replayed": len(tasks)}


def run_recovery(api, mode, **arguments):
    if mode == "create":
        result = create_issues(api, arguments["records"])
    elif mode == "search":
        result = search_issues(api, arguments["queries"])
    elif mode == "update":
        result = update_issues(api, arguments["updates"])
    elif mode == "webhooks":
        result = create_webhooks(api, **arguments)
    elif mode == "catalog":
        result = create_catalog(api, **arguments)
    elif mode == "repository":
        result = create_repository(api, **arguments)
    elif mode == "push":
        result = push_files(api, **arguments)
    elif mode == "read":
        result = read_files(api, **arguments)
    elif mode == "adopt":
        result = adopt_repositories(api, **arguments)
    elif mode == "sync-branches":
        result = sync_branches()
    elif mode == "replay-webhooks":
        result = replay_webhooks(**arguments)
    elif mode == "member":
        result = add_member(api, **arguments)
    elif mode in {"replay", "verify"}:
        result = reconcile_issues(api, arguments["receipts"], write=mode == "replay")
    else:
        raise ValueError(mode)
    print(json.dumps(result))
