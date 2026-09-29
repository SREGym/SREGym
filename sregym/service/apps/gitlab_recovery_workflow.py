"""Synthetic GitLab issue receipts; replay preserves public project/IID identity."""

import json


def reconcile(client, receipts, write=False):
    created = 0
    for receipt in receipts:
        prefix = f"/api/v4/projects/{receipt['project']}/issues"
        try:
            issue = client.call("GET", f"{prefix}/{receipt['iid']}")
        except RuntimeError as exc:
            if "HTTP 404:" not in str(exc):
                raise
            issue = None
        if issue is None and write:
            issue = client.call("POST", prefix, {k: receipt[k] for k in ("title", "description", "confidential")})
            created += 1
        if issue is None or any(issue.get(k) != receipt[k] for k in ("iid", "title", "description", "confidential")):
            raise RuntimeError(f"Unreconciled or conflicting issue: project {receipt['project']} #{receipt['iid']}")
    return {"verified": len(receipts), "created": created}


def recovery_workflow(client, mode, **args):
    if mode == "seed":
        projects = []
        for index in range(1, args["count"] + 1):
            username = f"incident-user-{index}"
            user = client.call(
                "POST",
                "/api/v4/users",
                {
                    "username": username,
                    "name": f"Incident User {index}",
                    "email": username + "@sregym.local",
                    "password": client.password,
                    "skip_confirmation": True,
                },
            )
            project = client.call(
                "POST",
                "/api/v4/projects",
                {
                    "name": f"incident-{index}",
                    "path": f"incident-{index}",
                    "visibility": "private",
                    "initialize_with_readme": True,
                },
            )
            client.call(
                "POST",
                f"/api/v4/projects/{project['id']}/members",
                {
                    "user_id": user["id"],
                    "access_level": 30,
                },
            )
            projects.append(project["id"])
        result = projects
    elif mode == "create":
        result = []
        for record in args["records"]:
            issue = client.call(
                "POST",
                f"/api/v4/projects/{record['project']}/issues",
                {k: record[k] for k in ("title", "description", "confidential")},
            )
            result.append({**record, "iid": issue["iid"]})
    elif mode in ("replay", "verify"):
        result = reconcile(client, args["receipts"], write=mode == "replay")
    elif mode == "git":
        result = {}
        for project in client.call("GET", "/api/v4/projects?owned=true&per_page=100"):
            prefix = f"/api/v4/projects/{project['id']}/repository/tree?recursive=true&per_page=100&page="
            files = {}
            page = 1
            while True:
                entries = client.call("GET", prefix + str(page))
                files.update({entry["path"]: entry["id"] for entry in entries if entry["type"] == "blob"})
                if len(entries) < 100:
                    break
                page += 1
            result[str(project["id"])] = files
    else:
        raise ValueError(mode)
    print(json.dumps(result))
