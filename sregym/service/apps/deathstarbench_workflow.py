"""Business-operation probe executed inside a cluster client pod (stdlib only)."""

import base64
import http.cookiejar
import json
import secrets
import sys
import time
import urllib.parse
import urllib.request


def check(application):
    cookies = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookies))
    token = secrets.token_hex(12)

    def request(base, path, values=None, post=False):
        query = urllib.parse.urlencode(values or {})
        url = base + path + ("?" + query if query and not post else "")
        data = query.encode() if post else None
        with opener.open(url, data=data, timeout=15) as response:
            return response.read().decode(), response.headers

    if application == "hotel-reservation":
        base = "http://frontend:5000"
        response, _ = request(
            base, "/hotels", {"inDate": "2015-04-09", "outDate": "2015-04-10", "lat": "37.7867", "lon": "-122.4112"}
        )
        assert json.loads(response)["features"], "hotel search returned no inventory"
        response, _ = request(
            base,
            "/reservation",
            {
                "inDate": "2030-01-01",
                "outDate": "2030-01-02",
                "hotelId": "1",
                "customerName": token,
                "username": "Cornell_0",
                "password": "0" * 10,
                "number": "1",
            },
        )
        assert json.loads(response)["message"] == "Reserve successfully!", response
    elif application == "social-network":
        base = "http://nginx-thrift:8080"
        user = "probe_" + token
        # The wrk2 registration API accepts an explicit ID. Keep it exactly representable in Lua numbers.
        expected_user_id = int(token[:12], 16) + 1000
        request(
            base,
            "/wrk2-api/user/register",
            {
                "first_name": "Probe",
                "last_name": "User",
                "username": user,
                "password": token,
                "user_id": expected_user_id,
            },
            post=True,
        )
        request(base, "/api/user/login", {"username": user, "password": token}, post=True)
        # Login sets the cookie on a redirect, so the final page's headers do
        # not contain it. The opener's cookie jar retains the authenticated session.
        jwt = next((cookie.value for cookie in cookies if cookie.name == "login_token"), None)
        assert jwt, "login did not establish a session"
        payload = jwt.split(".")[1]
        user_id = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))["user_id"]
        assert int(user_id) == expected_user_id
        # Exercise a connected user: the upstream home-timeline fanout path
        # rejects an empty recipient set. The seeded account gives this post a recipient.
        request(base, "/wrk2-api/user/follow", {"user_id": 0, "followee_id": user_id}, post=True)
        request(
            base,
            "/wrk2-api/post/compose",
            {
                "username": user,
                "user_id": user_id,
                "text": token,
                "media_ids": "[]",
                "media_types": "[]",
                "post_type": "0",
            },
            post=True,
        )
        for _ in range(20):
            response, _ = request(base, "/wrk2-api/user-timeline/read", {"user_id": user_id, "start": 0, "stop": 10})
            posts = json.loads(response)
            if isinstance(posts, list) and any(post.get("text") == token for post in posts):
                break
            time.sleep(1)
        else:
            raise AssertionError("new post did not reach the user timeline")
    else:
        raise ValueError(application)
    print(json.dumps({"success": True, "application": application, "token": token}))


if __name__ == "__main__":
    check(sys.argv[1])
