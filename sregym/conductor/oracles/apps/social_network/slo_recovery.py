"""Social Network writes, timeline reads, and durable-data preservation."""

import json
import time
import uuid

from sregym.conductor.oracles.application_recovery import ApplicationRecoveryCheck


class SocialNetworkRecoveryCheck(ApplicationRecoveryCheck):
    collections = {
        "users": ("user-mongodb", "user", "user"),
        "posts": ("post-storage-mongodb", "post", "post"),
    }

    def verify_requests(self, url, records):
        nonce = uuid.uuid4().hex
        author_id = 1_000_000_000 + int(nonce[:10], 16)
        reader_id = author_id + 1
        author = "member_" + nonce
        reader = "reader_" + nonce
        for ident, username in ((author_id, author), (reader_id, reader)):
            response = self.request(
                url,
                "/wrk2-api/user/register",
                {"user_id": ident, "username": username, "password": nonce, "first_name": "New", "last_name": "Member"},
                post=True,
            )
            if response.strip() != "Success!":
                raise ValueError("User registration failed")
        followed = self.request(
            url, "/wrk2-api/user/follow", {"user_id": reader_id, "followee_id": author_id}, post=True
        )
        if followed.strip() != "Success!":
            raise ValueError("Following a user failed")
        text = "Service update " + nonce
        response = self.request(
            url,
            "/wrk2-api/post/compose",
            {
                "username": author,
                "user_id": str(author_id),
                "text": text,
                "post_type": "0",
                "media_ids": "[]",
                "media_types": "[]",
            },
            post=True,
        )
        if "Successfully upload post" not in response:
            raise ValueError("Post creation did not succeed")
        stored = self.read_collection("posts", {"text": {"$regex": nonce}})
        if len(stored) != 1 or nonce not in stored[0].get("text", ""):
            raise ValueError("Post response has no matching persistent record")
        # Timeline fan-out is asynchronous. Observe the new post rather than
        # accepting an empty, unrelated, or stale successful response.
        pending = {"user-timeline": str(author_id), "home-timeline": str(reader_id)}
        deadline = time.monotonic() + 20
        while pending and time.monotonic() < deadline:
            for endpoint, user in list(pending.items()):
                body = json.loads(
                    self.request(url, f"/wrk2-api/{endpoint}/read", {"user_id": user, "start": 0, "stop": 1000})
                )
                # Lua cjson encodes an empty timeline table as an object.
                if body == {}:
                    body = []
                if not isinstance(body, list):
                    raise ValueError(f"{endpoint} did not return a post list")
                if any(
                    isinstance(post, dict)
                    and nonce in post.get("text", "")
                    and post.get("creator", {}).get("username") == author
                    for post in body
                ):
                    del pending[endpoint]
            if pending:
                time.sleep(1)
        if pending:
            raise ValueError("Fresh post is absent from " + ", ".join(sorted(pending)))
