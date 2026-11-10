"""The provider's instance inventory, with an account-wide instance quota.

Launching an instance takes a place under the quota, and the place is held for
as long as the instance exists, whether or not it ever serves: the provider
neither knows nor cares whether the customer finished configuring it. A new
instance is pending while it boots, and is running only after that. A place
comes back only when the instance is gone, which the inventory learns by
listing the customer's instances (gateway pods) through the Kubernetes API.

The quota is read from a local file that only the provider (the harness,
through ``kubectl exec``) writes, and counters are published the same way.
"""

import json
import os
import ssl
import threading
import time
import urllib.error
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request, urlopen

STATE = Path(os.environ.get("COMPUTE_STATE", "/run/compute"))
INITIAL_QUOTA = int(os.environ.get("COMPUTE_QUOTA", "20"))
NAMESPACE = os.environ.get("COMPUTE_NAMESPACE", "mattermost")
SELECTOR = os.environ.get("COMPUTE_SELECTOR", "app=chat-gateway")
RECONCILE_SECONDS = float(os.environ.get("COMPUTE_RECONCILE_SECONDS", "5"))
BOOT_SECONDS = float(os.environ.get("COMPUTE_BOOT_SECONDS", "0"))
API = "https://kubernetes.default.svc"
ACCOUNT = Path("/var/run/secrets/kubernetes.io/serviceaccount")


class Inventory:
    def __init__(self, quota, boot_seconds=0.0, clock=time.monotonic):
        self.quota = quota
        self.boot_seconds = boot_seconds
        self.clock = clock
        self.instances = {}
        self.refused = 0
        self.lock = threading.Lock()

    def in_use(self):
        with self.lock:
            return len(self.instances)

    def launch(self, instance):
        """None when the instance is running, otherwise why it is not."""
        with self.lock:
            if instance not in self.instances:
                if len(self.instances) >= self.quota:
                    self.refused += 1
                    return f"instance quota exceeded: {len(self.instances)} of {self.quota} instances in use"
                self.instances[instance] = self.clock()
            if self.clock() - self.instances[instance] < self.boot_seconds:
                return f"instance {instance} is pending"
            return None

    def reconcile(self, live):
        """Release the places of instances that no longer exist; None means unknown."""
        if live is None:
            return
        with self.lock:
            for instance in [name for name in self.instances if name not in live]:
                del self.instances[instance]


def credentials():
    token = (ACCOUNT / "token").read_text()
    return token, ssl.create_default_context(cafile=str(ACCOUNT / "ca.crt"))


def live_instances(namespace, selector):
    try:
        token, context = credentials()
        query = urllib.parse.urlencode({"labelSelector": selector})
        request = Request(
            f"{API}/api/v1/namespaces/{namespace}/pods?{query}", headers={"Authorization": f"Bearer {token}"}
        )
        with urlopen(request, timeout=10, context=context) as response:
            items = json.loads(response.read())["items"]
    except (OSError, ValueError, KeyError, urllib.error.URLError):
        return None
    return {item["metadata"]["name"] for item in items}


inventory = Inventory(INITIAL_QUOTA, BOOT_SECONDS)


def read_quota(default):
    try:
        return int(float((STATE / "quota").read_text().strip()))
    except (OSError, ValueError):
        return default


def maintain():
    while True:
        inventory.quota = read_quota(inventory.quota)
        inventory.reconcile(live_instances(NAMESPACE, SELECTOR))
        with inventory.lock:
            snapshot = {
                "at": time.time(),
                "quota": inventory.quota,
                "in_use": len(inventory.instances),
                "refused": inventory.refused,
                "instances": sorted(inventory.instances),
            }
        temporary = STATE / "stats.json.tmp"
        temporary.write_text(json.dumps(snapshot))
        temporary.replace(STATE / "stats.json")
        time.sleep(RECONCILE_SECONDS)


class Compute(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        return

    def reply(self, code, body):
        payload = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        if self.path == "/health":
            return self.reply(200, {"status": "ok"})
        return self.reply(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/instances":
            return self.reply(404, {"error": "not found"})
        length = int(self.headers.get("Content-Length") or 0)
        instance = json.loads(self.rfile.read(length) or b"{}").get("instance")
        if not instance:
            return self.reply(400, {"error": "instance required"})
        refusal = inventory.launch(instance)
        if refusal:
            return self.reply(409, {"error": refusal})
        return self.reply(200, {"instance": instance})


if __name__ == "__main__":
    STATE.mkdir(parents=True, exist_ok=True)
    if not (STATE / "quota").exists():
        (STATE / "quota").write_text(f"{INITIAL_QUOTA}\n")
    threading.Thread(target=maintain, daemon=True).start()
    ThreadingHTTPServer(("", 8080), Compute).serve_forever()
