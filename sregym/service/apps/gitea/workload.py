"""Gitea API seeding and business probes, executed inside the client pod."""

import base64
import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REPOSITORY = "zoo-labs/zoo-utilities"


def api(path, method="GET", data=None, missing_ok=False):
    username = Path("/credentials/username").read_text().strip()
    password = Path("/credentials/password").read_text().strip()
    auth = base64.b64encode(f"{username}:{password}".encode()).decode()
    request = urllib.request.Request(
        "http://gitea:3000/api/v1" + path,
        data=json.dumps(data).encode() if data is not None else None,
        headers={"Authorization": "Basic " + auth, "Content-Type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = response.read()
            return json.loads(payload) if payload else None
    except urllib.error.HTTPError as exc:
        if missing_ok and exc.code == 404:
            return None
        raise RuntimeError(f"Gitea {method} {path}: HTTP {exc.code}: {exc.read().decode()[:300]}") from exc


def file_path(repository, name):
    return f"/repos/{repository}/contents/" + urllib.parse.quote(name, safe="/")


def write_file(repository, name, content):
    path = file_path(repository, name)
    old = api(path, missing_ok=True)
    data = {"content": base64.b64encode(content.encode()).decode(), "message": "Benchmark data: " + name}
    if old:
        data["sha"] = old["sha"]
    return api(path, method="PUT" if old else "POST", data=data)


def read_file(repository, name):
    return base64.b64decode(api(file_path(repository, name))["content"]).decode()


def seed(fixture):
    for user in fixture["users"]:
        api(
            "/admin/users",
            "POST",
            {
                "username": user["username"],
                "email": user["email"],
                "password": user["password"],
                "full_name": user["full_name"],
                "must_change_password": False,
                "send_notify": False,
            },
        )
    organizations = {org["name"] for org in fixture["organizations"]}
    for org in fixture["organizations"]:
        api(
            "/orgs",
            "POST",
            {
                "username": org["name"],
                "full_name": org["full_name"],
                "description": org["description"],
                "visibility": org["visibility"],
            },
        )
    for repository in fixture["repositories"]:
        owner = repository["owner"]
        endpoint = f"/orgs/{owner}/repos" if owner in organizations else f"/admin/users/{owner}/repos"
        api(
            endpoint,
            "POST",
            {
                "name": repository["name"],
                "description": repository["description"],
                "private": repository["private"],
                "auto_init": True,
                "default_branch": "main",
            },
        )
        full_name = owner + "/" + repository["name"]
        for name, content in {"README.md": repository["readme"], **repository["files"]}.items():
            write_file(full_name, name, content)
        api(
            f"/repos/{full_name}/issues",
            "POST",
            {
                "title": "Validate backup and restore for " + repository["name"],
                "body": "Verify repository contents and issue records after recovery.",
            },
        )
    for team in fixture["teams"]:
        created = api(
            f"/orgs/{team['org']}/teams",
            "POST",
            {
                "name": team["name"],
                "description": team["description"],
                "permission": team["permission"],
                "units_map": {
                    unit: team["permission"]
                    for unit in ("repo.code", "repo.issues", "repo.pulls", "repo.releases", "repo.wiki")
                },
                "includes_all_repositories": True,
            },
        )
        for user in team["members"]:
            api(f"/teams/{created['id']}/members/{user}", "PUT")
    return {"users": len(fixture["users"]), "repositories": len(fixture["repositories"])}


def run(mode, **arguments):
    if mode == "seed":
        result = seed(arguments["fixture"])
    elif mode == "probe":
        token = arguments["token"]
        title = "sregym-" + token
        issue = api(f"/repos/{REPOSITORY}/issues", "POST", {"title": title, "body": token})
        assert api(f"/repos/{REPOSITORY}/issues/{issue['number']}")["title"] == title
        write_file(REPOSITORY, f"sregym/{token}.txt", token)
        assert read_file(REPOSITORY, f"sregym/{token}.txt") == token
        result = {"token": token, "issue_number": issue["number"]}
    elif mode == "verify":
        token = arguments["token"]
        assert read_file(REPOSITORY, f"sregym/{token}.txt") == token
        assert api(f"/repos/{REPOSITORY}/issues/{arguments['issue_number']}")["title"] == "sregym-" + token
        for repository in arguments["fixture"]["repositories"]:
            full_name = repository["owner"] + "/" + repository["name"]
            for name, expected in {"README.md": repository["readme"], **repository["files"]}.items():
                assert read_file(full_name, name) == expected, f"Seeded repository file changed: {full_name}/{name}"
        result = {"success": True}
    else:
        raise ValueError(mode)
    print(json.dumps(result))
