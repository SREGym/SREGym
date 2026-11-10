"""Create the chat workload's people: colleagues on their phones, and people messaging them.

Run once by the harness in the application-client pod, against Mattermost
directly. Each phone user logs in once from an Android device (a session with a
device id) and never opens the conversations, so every direct message to them
is a push notification. Each sender gets a session token and direct-message
channels with a few phone users. Prints the workload as JSON on stdout.
"""

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

BACKEND = "http://mattermost:8065"
PASSWORD = Path("/credentials/password").read_text().strip()


def call(method, path, body=None, token=None):
    """``(parsed body, response headers)``; an existing user is not an error."""
    request = urllib.request.Request(
        BACKEND + path,
        data=None if body is None else json.dumps(body).encode(),
        method=method,
        headers={"Content-Type": "application/json", **({"Authorization": "Bearer " + token} if token else {})},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read() or b"null"), response.headers


def ensure_user(name):
    try:
        call("POST", "/api/v4/users", {"email": f"{name}@example.com", "username": name, "password": PASSWORD})
    except urllib.error.HTTPError as exc:
        if exc.code not in (400, 409):
            raise


def login(name, device_id=None):
    body = {"login_id": name, "password": PASSWORD, **({"device_id": device_id} if device_id else {})}
    user, headers = call("POST", "/api/v4/users/login", body)
    return user["id"], headers["Token"]


def seed(phones, senders, per_sender, base):
    phone_ids = []
    for index in range(phones):
        name = f"phone-{index + 1:02d}"
        ensure_user(name)
        user_id, _ = login(name, device_id=f"android_rn-v2:fcm-device-{index + 1:04d}")
        phone_ids.append(user_id)
    workload = {"base": base, "senders": []}
    for index in range(senders):
        name = f"desk-{index + 1:02d}"
        ensure_user(name)
        user_id, token = login(name)
        channels = []
        for offset in range(per_sender):
            peer = phone_ids[(index * per_sender + offset) % len(phone_ids)]
            channel, _ = call("POST", "/api/v4/channels/direct", [user_id, peer], token=token)
            channels.append(channel["id"])
        workload["senders"].append({"user_id": user_id, "token": token, "channels": channels})
    return workload


if __name__ == "__main__":
    phones, senders, per_sender, base = int(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3]), sys.argv[4]
    print(json.dumps(seed(phones, senders, per_sender, base)))
