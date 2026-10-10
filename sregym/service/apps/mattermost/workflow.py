"""Mattermost seed, functional probe, and durability checks."""


def workflow(c, mode, token=None, **baseline):
    if mode == "seed":
        c.call(
            "POST",
            "/api/v4/users",
            {"email": "operations-bot@example.com", "username": "operations-bot", "password": c.password},
        )
    user = c.call("POST", "/api/v4/users/login", {"login_id": "operations-bot", "password": c.password})
    c.headers["Authorization"] = "Bearer " + c.response_headers["Token"]
    if mode == "seed":
        team = c.call("POST", "/api/v4/teams", {"name": "operations", "display_name": "Operations", "type": "O"})
        c.call("POST", "/api/v4/teams/" + team["id"] + "/members", {"team_id": team["id"], "user_id": user["id"]})
        channel = c.call(
            "POST",
            "/api/v4/channels",
            {"team_id": team["id"], "name": "incidents", "display_name": "Incidents", "type": "O"},
        )
        c.call(
            "POST",
            "/api/v4/posts",
            {
                "channel_id": channel["id"],
                "message": "Runbook: retain messages and attachments during database recovery.",
            },
        )
        return {"team": team["id"], "channel": channel["id"]}
    team = c.call("GET", "/api/v4/teams/name/operations")
    channel = c.call("GET", "/api/v4/teams/" + team["id"] + "/channels/name/incidents")
    if mode == "probe":
        boundary = "attachment" + token
        body = (
            f'--{boundary}\r\nContent-Disposition: form-data; name="channel_id"\r\n\r\n{channel["id"]}\r\n'
            f'--{boundary}\r\nContent-Disposition: form-data; name="files"; filename="{token}.txt"\r\n'
            f"Content-Type: text/plain\r\n\r\n{token}\r\n--{boundary}--\r\n"
        ).encode()
        upload = c.call("POST", "/api/v4/files", body, {"Content-Type": f"multipart/form-data; boundary={boundary}"})
        fid = upload["file_infos"][0]["id"]
        post = c.call(
            "POST", "/api/v4/posts", {"channel_id": channel["id"], "message": "change-" + token, "file_ids": [fid]}
        )
        return {"token": token, "post": post["id"], "file": fid, "channel": channel["id"]}
    post = c.call("GET", "/api/v4/posts/" + baseline["post"])
    c.require(post["message"] == "change-" + token and not post["delete_at"], "Acknowledged message changed")
    c.require(
        post["channel_id"] == baseline["channel"] and baseline["file"] in post["file_ids"], "Message attachment changed"
    )
    c.require(
        c.call("GET", "/api/v4/files/" + baseline["file"], raw=True).decode() == token, "Attachment content changed"
    )
    return {"verified": True}
