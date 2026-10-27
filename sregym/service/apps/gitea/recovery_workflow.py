"""Issue journal operations, also executed with the real API inside the client pod."""

import json
import urllib.parse


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
                "events": ["issues", "issue_comment"],
                "config": {"url": url, "content_type": "json", "secret": secret},
            },
        )
        created += 1
    return {"created": created}


def update_issues(api, updates):
    """Comment on or close existing issues; comment receipts carry the comment id."""
    receipts = []
    for update in updates:
        path = f"/repos/{update['repository']}/issues/{update['number']}"
        if "comment" in update:
            comment = api(path + "/comments", "POST", {"body": update["comment"]})
            receipts.append({**update, "comment_id": comment["id"]})
        else:
            api(path, "PATCH", {"state": "closed"})
            receipts.append(update)
    return receipts


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


def run_recovery(api, mode, **arguments):
    if mode == "create":
        result = create_issues(api, arguments["records"])
    elif mode == "search":
        result = search_issues(api, arguments["queries"])
    elif mode == "update":
        result = update_issues(api, arguments["updates"])
    elif mode == "webhooks":
        result = create_webhooks(api, **arguments)
    elif mode == "member":
        result = add_member(api, **arguments)
    elif mode in {"replay", "verify"}:
        result = reconcile_issues(api, arguments["receipts"], write=mode == "replay")
    else:
        raise ValueError(mode)
    print(json.dumps(result))
