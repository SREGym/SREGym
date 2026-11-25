"""Workspace client workflows: Mattermost's, plus provisioning of chat users.

Concatenated after the Mattermost workflow, whose `workflow` it extends.
"""

_mattermost_workflow = workflow  # noqa: F821 - defined by the preceding source


def workflow(c, mode, **arguments):
    if mode != "chat-users":
        return _mattermost_workflow(c, mode, **arguments)
    tokens = []
    for i in range(1, arguments["count"] + 1):
        name = f"member{i:03d}"
        try:
            c.call(
                "POST", "/api/v4/users", {"email": f"{name}@tenant.example", "username": name, "password": c.password}
            )
        except RuntimeError as error:
            if "HTTP 400" not in str(error):
                raise
        c.headers.pop("Authorization", None)
        c.call("POST", "/api/v4/users/login", {"login_id": name, "password": c.password})
        tokens.append(c.response_headers["Token"])
    return {"tokens": tokens}
