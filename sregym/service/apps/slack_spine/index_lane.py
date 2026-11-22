"""Shared broker and HTTP probes for Slack Spine's search-index lane."""

from __future__ import annotations

import json
import shlex
import uuid
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sregym.service.apps.slack_spine.application import SlackSpine
    from sregym.service.kubectl import KubeCtl

# Runs in the ops toolbox (stdlib only): post messages through svc-message,
# then look them up through svc-search. ``must`` ids are re-queried for up to
# ``wait_s`` seconds because the engine publishes new documents asynchronously.
_TOOLBOX_PROBE = r"""
import json, sys, time, urllib.parse, urllib.request

spec = json.loads(sys.argv[1])


def call(url, body=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method="GET" if body is None else "POST",
                                 headers={"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.status, resp.read().decode()


posted = []
for message in spec.get("post", []):
    status, body = call(spec["message_url"] + "/messages", message)
    if not 200 <= status < 300:
        raise SystemExit("send %s returned %s: %s" % (message["client_msg_id"], status, body[:200]))
    posted.append(message["client_msg_id"])


def found(doc_id):
    query = urllib.parse.urlencode({"q": doc_id, "org_id": spec["org_id"]})
    status, body = call(spec["search_url"] + "/search?" + query)
    if not 200 <= status < 300:
        raise SystemExit("search returned %s: %s" % (status, body[:200]))
    return any(isinstance(h, dict) and h.get("id") == doc_id for h in json.loads(body).get("hits", []))


hits = {doc_id for doc_id in spec.get("search", []) if found(doc_id)}
must = [doc_id for doc_id in spec.get("must", []) if doc_id not in hits]
deadline = time.time() + float(spec.get("wait_s", 0))
while must and time.time() < deadline:
    time.sleep(1)
    hits.update(doc_id for doc_id in must if found(doc_id))
    must = [doc_id for doc_id in must if doc_id not in hits]
print("PROBE=" + json.dumps({"posted": posted, "found": sorted(hits)}))
"""


class IndexLaneOperations:
    """Operations used by queue faults with an explicit worker/probe context."""

    app: SlackSpine
    kubectl: KubeCtl
    namespace: str
    channel_id: str
    org_id: str
    run_tag: str
    PROBE_PREFIX: str
    BROKER_SELECTOR: str
    CONSUMER_DEPLOYMENT: str

    def _broker_pod(self) -> str:
        pods = self.app.pod_names(self.BROKER_SELECTOR)
        if not pods:
            raise RuntimeError("no running Redpanda broker pod found")
        return pods[0]

    def rpk(self, command: str, timeout: float = 60) -> str:
        return self.app.exec_in(f"pod/{self._broker_pod()}", f"rpk {command}", container="redpanda", timeout=timeout)

    def _toolbox_probe(self, spec: dict) -> dict:
        spec = {"message_url": "http://svc-message:8000", "search_url": "http://svc-search:8000", **spec}
        out = self.app.toolbox_exec(f"python3 - {shlex.quote(json.dumps(spec))}", input_data=_TOOLBOX_PROBE, timeout=90)
        for line in out.splitlines():
            if line.startswith("PROBE="):
                return json.loads(line.removeprefix("PROBE="))
        raise RuntimeError(f"toolbox probe returned no result: {out[-500:]!r}")

    def _message(self, doc_id: str, text: str, **extra) -> dict:
        return {"channel_id": self.channel_id, "client_msg_id": doc_id, "text": f"{text} {doc_id}", **extra}

    def send_probes(self, count: int) -> list[str]:
        ids = [f"{self.PROBE_PREFIX}{self.run_tag}{uuid.uuid4().hex[:12]}" for _ in range(count)]
        self._toolbox_probe({"post": [self._message(i, "search freshness check") for i in ids], "org_id": self.org_id})
        return ids

    def search(self, ids: list[str], must: list[str] = (), wait_s: float = 0) -> set[str]:
        result = self._toolbox_probe({"search": ids, "must": list(must), "wait_s": wait_s, "org_id": self.org_id})
        return set(result["found"])

    def _rollout(self) -> None:
        self.kubectl.exec_command_checked(
            f"kubectl rollout status deployment/{self.CONSUMER_DEPLOYMENT} -n {self.namespace} --timeout=300s",
            timeout=330,
        )
