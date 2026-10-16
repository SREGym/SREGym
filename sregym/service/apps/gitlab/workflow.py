"""Gitlab seed, functional probe, and durability checks."""

import base64
import urllib.parse


def workflow(c, mode, token=None, **baseline):
    c.headers["PRIVATE-TOKEN"] = c.token
    project_path = "/api/v4/projects/root%2Foperations"
    if mode == "seed":
        p = c.call(
            "POST",
            "/api/v4/projects",
            {"name": "operations", "path": "operations", "visibility": "private", "initialize_with_readme": True},
        )
        c.call(
            "POST",
            f"/api/v4/projects/{p['id']}/issues",
            {
                "title": "Review release checklist",
                "description": "Confirm release owners and the deployment schedule.",
            },
        )
        return {"project": p["id"]}
    p = c.call("GET", project_path)
    prefix = f"/api/v4/projects/{p['id']}"
    if mode == "probe":
        issue = c.call("POST", prefix + "/issues", {"title": "healthcheck-" + token, "description": token})
        path = "healthchecks/" + token + ".txt"
        c.call(
            "POST",
            prefix + "/repository/files/" + urllib.parse.quote(path, safe=""),
            {"branch": p["default_branch"], "content": token, "commit_message": "Update healthcheck " + token},
        )
        return {"token": token, "issue": issue["iid"], "project": p["id"], "file": path, "branch": p["default_branch"]}
    c.require(p["id"] == baseline["project"], "Original GitLab project was replaced")
    issue = c.call("GET", prefix + f"/issues/{baseline['issue']}")
    c.require(issue["title"] == "healthcheck-" + token and issue["description"] == token, "Acknowledged issue changed")
    content = c.call(
        "GET",
        prefix
        + "/repository/files/"
        + urllib.parse.quote(baseline["file"], safe="")
        + "?ref="
        + urllib.parse.quote(baseline["branch"], safe=""),
    )
    c.require(base64.b64decode(content["content"]).decode() == token, "Acknowledged Git file changed")
    return {"verified": True}
