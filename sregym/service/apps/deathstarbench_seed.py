"""Seed the users expected by SocialNetwork's upstream mixed workload.

Run only during initial environment setup, before workload or fault injection.
Uses the real application APIs and a deterministic ring graph; no external data
downloads or synthetic database inserts are needed.
"""

import concurrent.futures
import json
import urllib.parse
import urllib.request

USERS = 962
BASE = "http://nginx-thrift:8080/wrk2-api"


def post(path, values):
    data = urllib.parse.urlencode(values).encode()
    with urllib.request.urlopen(BASE + path, data=data, timeout=30) as response:
        body = response.read().decode().strip()
        if body and not body.startswith("Success"):
            raise RuntimeError(f"Seed request {path} failed: {body[:200]}")


def register(index):
    post(
        "/user/register",
        {
            "first_name": "first_name_" + str(index),
            "last_name": "last_name_" + str(index),
            "username": "username_" + str(index),
            "password": "password_" + str(index),
            "user_id": index,
        },
    )


def follow(index):
    for offset in (1, 2):
        post("/user/follow", {"user_id": index, "followee_id": (index + offset) % USERS})


def compose(index):
    post(
        "/post/compose",
        {
            "username": "username_" + str(index),
            "user_id": index,
            "text": "sregym-seed-" + str(index),
            "media_ids": "[]",
            "media_types": "[]",
            "post_type": 0,
        },
    )


if __name__ == "__main__":
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        # Complete each dependency stage and surface every request failure.
        for operation in (register, follow, compose):
            list(pool.map(operation, range(USERS)))
    print(json.dumps({"users": USERS, "follows": 2 * USERS, "posts": USERS}))
