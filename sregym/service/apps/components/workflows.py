"""Real business probes, executed inside the isolated application-client pod."""

import base64
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


class Client:
    def __init__(self, slug):
        self.slug = slug
        self.base = {
            "gitlab-ce": "http://gitlab-ce",
            "mattermost": "http://mattermost:8065",
            "stripe-marathon": "http://stripe-marathon:8000",
        }[slug]
        self.headers = {}
        self.password = Path("/credentials/password").read_text()
        self.token = Path("/credentials/token").read_text()
        if slug == "gitlab-ce":
            self.headers["PRIVATE-TOKEN"] = self.token
        elif slug == "stripe-marathon":
            self.headers["Authorization"] = "Bearer " + self.token

    def call(self, method, path, data=None, headers=None, raw=False):
        hs = {**self.headers, **(headers or {})}
        if data is not None and not isinstance(data, bytes):
            data = json.dumps(data).encode()
            hs.setdefault("Content-Type", "application/json")
        req = urllib.request.Request(self.base + path, data=data, headers=hs, method=method)
        try:
            with urllib.request.urlopen(req, timeout=20) as response:
                body = response.read()
                self.response_headers = response.headers
                return body if raw else json.loads(body) if body else None
        except urllib.error.HTTPError as exc:
            # Retain API evidence, never headers containing access credentials.
            detail = exc.read().decode(errors="replace")[:1000]
            raise RuntimeError(f"{method} {path}: HTTP {exc.code}: {detail}") from None


def gitlab(c, mode, token=None, **baseline):
    project_path = "/api/v4/projects/root%2Fsregym"
    if mode == "seed":
        p = c.call(
            "POST",
            "/api/v4/projects",
            {"name": "sregym", "path": "sregym", "visibility": "private", "initialize_with_readme": True},
        )
        c.call(
            "POST",
            f"/api/v4/projects/{p['id']}/issues",
            {
                "title": "Database recovery runbook",
                "description": "Preserve acknowledged issues and Git history while recovering database access.",
            },
        )
        return {"project": p["id"]}
    p = c.call("GET", project_path)
    prefix = f"/api/v4/projects/{p['id']}"
    if mode == "probe":
        issue = c.call("POST", prefix + "/issues", {"title": "sregym-" + token, "description": token})
        path = "sregym/" + token + ".txt"
        c.call(
            "POST",
            prefix + "/repository/files/" + urllib.parse.quote(path, safe=""),
            {"branch": p["default_branch"], "content": token, "commit_message": "Durability probe " + token},
        )
        return {"token": token, "issue": issue["iid"], "project": p["id"], "file": path, "branch": p["default_branch"]}
    require(p["id"] == baseline["project"], "Original GitLab project was replaced")
    issue = c.call("GET", prefix + f"/issues/{baseline['issue']}")
    require(issue["title"] == "sregym-" + token and issue["description"] == token, "Acknowledged issue changed")
    content = c.call(
        "GET",
        prefix
        + "/repository/files/"
        + urllib.parse.quote(baseline["file"], safe="")
        + "?ref="
        + urllib.parse.quote(baseline["branch"], safe=""),
    )
    require(base64.b64decode(content["content"]).decode() == token, "Acknowledged Git file changed")
    return {"verified": True}


def mattermost(c, mode, token=None, **baseline):
    if mode == "seed":
        c.call(
            "POST",
            "/api/v4/users",
            {"email": "benchmark@sregym.local", "username": "benchmark", "password": c.password},
        )
    user = c.call("POST", "/api/v4/users/login", {"login_id": "benchmark", "password": c.password})
    c.headers["Authorization"] = "Bearer " + c.response_headers["Token"]
    if mode == "seed":
        team = c.call("POST", "/api/v4/teams", {"name": "sregym", "display_name": "Incident response", "type": "O"})
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
    team = c.call("GET", "/api/v4/teams/name/sregym")
    channel = c.call("GET", "/api/v4/teams/" + team["id"] + "/channels/name/incidents")
    if mode == "probe":
        boundary = "sregym" + token
        body = (
            f'--{boundary}\r\nContent-Disposition: form-data; name="channel_id"\r\n\r\n{channel["id"]}\r\n'
            f'--{boundary}\r\nContent-Disposition: form-data; name="files"; filename="{token}.txt"\r\n'
            f"Content-Type: text/plain\r\n\r\n{token}\r\n--{boundary}--\r\n"
        ).encode()
        upload = c.call("POST", "/api/v4/files", body, {"Content-Type": f"multipart/form-data; boundary={boundary}"})
        fid = upload["file_infos"][0]["id"]
        post = c.call(
            "POST", "/api/v4/posts", {"channel_id": channel["id"], "message": "sregym-" + token, "file_ids": [fid]}
        )
        return {"token": token, "post": post["id"], "file": fid, "channel": channel["id"]}
    post = c.call("GET", "/api/v4/posts/" + baseline["post"])
    require(post["message"] == "sregym-" + token and not post["delete_at"], "Acknowledged message changed")
    require(
        post["channel_id"] == baseline["channel"] and baseline["file"] in post["file_ids"], "Message attachment changed"
    )
    require(
        c.call("GET", "/api/v4/files/" + baseline["file"], raw=True).decode() == token, "Attachment content changed"
    )
    return {"verified": True}


def stripe(c, mode, token=None, wait_webhook=True, webhook_url="http://stripe-receiver:8080/hook", **baseline):
    if mode == "seed":
        endpoint = c.call(
            "POST",
            "/v1/webhook_endpoints",
            {"url": webhook_url, "enabled_events": ["payment_intent.succeeded"]},
        )
        return {"endpoint": endpoint["id"]}
    if mode == "probe":
        idem = {"Idempotency-Key": "customer-" + token}
        params = {"email": token + "@sregym.local", "metadata": {"probe": token}}
        customer = c.call("POST", "/v1/customers", params, idem)
        require(
            c.call("POST", "/v1/customers", params, idem)["id"] == customer["id"],
            "Idempotency created duplicate customers",
        )
        pm = c.call(
            "POST",
            "/v1/payment_methods",
            {"type": "card", "card": {"number": "4242424242424242", "exp_month": 12, "exp_year": 2030, "cvc": "123"}},
        )
        payment_params = {
            "amount": 2500,
            "currency": "usd",
            "customer": customer["id"],
            "payment_method": pm["id"],
            "confirm": True,
        }
        pi = c.call("POST", "/v1/payment_intents", payment_params, {"Idempotency-Key": "payment-" + token})
        require(pi["status"] == "succeeded", "Payment was not captured")
        refund = c.call(
            "POST", "/v1/refunds", {"payment_intent": pi["id"], "amount": 100}, {"Idempotency-Key": "refund-" + token}
        )
        result = {
            "token": token,
            "customer": customer["id"],
            "payment": pi["id"],
            "charge": pi["latest_charge"],
            "refund": refund["id"],
            "payment_params": payment_params,
        }
        if wait_webhook:
            stripe(c, "verify", **result)
        return result
    customer = c.call("GET", "/v1/customers/" + baseline["customer"])
    require(customer["email"] == token + "@sregym.local", "Acknowledged customer changed")
    pi = c.call("GET", "/v1/payment_intents/" + baseline["payment"])
    # The pinned reference subtracts refunds from amount_received.
    require(
        pi["status"] == "succeeded"
        and pi["amount"] == 2500
        and pi["currency"] == "usd"
        and pi["amount_received"] == 2400
        and pi["latest_charge"] == baseline["charge"],
        "Acknowledged payment changed",
    )
    charge = c.call("GET", "/v1/charges/" + baseline["charge"])
    require(charge["paid"] and charge["amount_captured"] == 2500, "Captured charge changed")
    refunds = charge["refunds"]["data"]
    require(
        charge["amount_refunded"] == 100
        and len(refunds) == 1
        and refunds[0]["id"] == baseline["refund"]
        and refunds[0]["amount"] == 100
        and refunds[0]["status"] == "succeeded",
        "Refund was lost or duplicated",
    )
    replay = c.call("POST", "/v1/payment_intents", baseline["payment_params"], {"Idempotency-Key": "payment-" + token})
    require(replay["id"] == pi["id"], "Restart lost idempotency and created another payment")
    deadline = time.monotonic() + 45
    while True:
        with urllib.request.urlopen("http://stripe-receiver:8080/receipts", timeout=5) as response:
            receipts = json.load(response)
        if any(x["data"]["object"]["id"] == pi["id"] for x in receipts if x["type"] == "payment_intent.succeeded"):
            break
        require(time.monotonic() < deadline, "Acknowledged payment webhook did not drain")
        time.sleep(1)
    return {"verified": True}


def run(slug, mode, **arguments):
    functions = {"gitlab-ce": gitlab, "mattermost": mattermost, "stripe-marathon": stripe}
    print(json.dumps(functions[slug](Client(slug), mode, **arguments)))
