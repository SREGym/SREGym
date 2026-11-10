"""Standard-library Infrahub API client for the conductor's probes.

Never stored in the cluster: probes send it, followed by ``workflow.py``, to the
application-client pod on stdin.
"""

import json
import os
import socket
import threading
import time
import urllib.error
import urllib.request

DEVICE_KIND = "NetworkDevice"
MODULE_KIND = "NetworkModule"
BASE_FIELDS = {DEVICE_KIND: ("role", "site"), MODULE_KIND: ("device", "slot")}
USER_AGENT = "infrahub-sdk-python/1.11.2"


def device_node(extra=(), absent=(), dropdowns=None):
    return _node("Device", "Network Device", ["role", "site"], extra, absent, dropdowns)


def module_node(extra=(), absent=()):
    return _node("Module", "Network Module", ["device", "slot"], extra, absent)


def _attribute(name, dropdowns):
    """A Text attribute, or a Dropdown with its choices when ``dropdowns`` lists ``name``."""
    if name in (dropdowns or {}):
        return {
            "name": name,
            "kind": "Dropdown",
            "optional": True,
            "choices": [{"name": c} for c in dropdowns[name]],
        }
    return {"name": name, "kind": "Text", "optional": True}


def _node(name, label, base, extra, absent, dropdowns=None):
    attributes = [{"name": "name", "kind": "Text", "unique": True}]
    attributes += [_attribute(a, dropdowns) for a in base]
    attributes += [_attribute(a, dropdowns) for a in extra]
    attributes += [{"name": a, "kind": "Text", "optional": True, "state": "absent"} for a in absent]
    return {
        "name": name,
        "namespace": "Network",
        "label": label,
        "include_in_menu": True,
        "human_friendly_id": ["name__value"],
        "attributes": attributes,
    }


class InfrahubError(RuntimeError):
    pass


class Infrahub:
    def __init__(self, base=None, token=None, timeout=60):
        self.base = (base or os.environ.get("INFRAHUB_ADDRESS") or "http://infrahub-server:8000").rstrip("/")
        if token is None:
            token = os.environ.get("INFRAHUB_API_TOKEN")
        if token is None:
            with open("/credentials/admin-token") as handle:
                token = handle.read().strip()
        self.token = token
        self.timeout = timeout

    def request(self, method, path, body=None, timeout=None):
        """Return ``(status, decoded body)``; HTTP errors are returned, not raised."""
        data = json.dumps(body).encode() if body is not None else None
        headers = {"X-INFRAHUB-KEY": self.token, "Content-Type": "application/json", "User-Agent": USER_AGENT}
        req = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        for attempt in range(6):
            try:
                with urllib.request.urlopen(req, timeout=timeout or self.timeout) as response:
                    raw = response.read()
                    status = response.status
                break
            except urllib.error.HTTPError as exc:
                raw, status = exc.read(), exc.code
                break
            except urllib.error.URLError as exc:
                # Name resolution or connection refused: the request never reached the server.
                if attempt == 5 or not isinstance(exc.reason, (socket.gaierror, ConnectionRefusedError)):
                    raise
                time.sleep(2)
        try:
            return status, json.loads(raw) if raw else None
        except ValueError:
            return status, raw.decode(errors="replace")[:2000]

    def graphql(self, query, variables=None, branch="main", timeout=None):
        """Return ``data``; raise InfrahubError with the server's messages on errors."""
        status, body = self.request(
            "POST", f"/graphql/{branch}", {"query": query, "variables": variables or {}}, timeout=timeout
        )
        if not isinstance(body, dict):
            raise InfrahubError(f"HTTP {status}: {body}")
        if body.get("errors"):
            raise InfrahubError("; ".join(str(e.get("message")) for e in body["errors"]))
        if status != 200:
            raise InfrahubError(f"HTTP {status}: {body}")
        return body.get("data") or {}

    # ---------------------------------------------------------------- schema
    def load_schema(self, nodes, branch="main", timeout=600):
        return self.request(
            "POST", f"/api/schema/load?branch={branch}", {"schemas": [{"version": "1.0", "nodes": nodes}]}, timeout
        )

    def load_concurrently(self, files, gap):
        """Send each schema file ``gap`` seconds after the previous one, concurrently; return the outcomes."""
        results = [None] * len(files)

        def send(index, nodes):
            started = time.time()
            status, body = self.load_schema(nodes)
            results[index] = {
                "status": status,
                "started": round(started, 3),
                "seconds": round(time.time() - started, 2),
                "error": None if status == 200 else str(body)[:300],
            }

        threads = []
        for index, nodes in enumerate(files):
            if index:
                time.sleep(gap)
            thread = threading.Thread(target=send, args=(index, nodes))
            thread.start()
            threads.append(thread)
        for thread in threads:
            thread.join()
        return results

    def kind_attributes(self, kind=DEVICE_KIND, branch="main"):
        status, body = self.request("GET", f"/api/schema/{kind}?branch={branch}", timeout=30)
        if status != 200 or not isinstance(body, dict):
            raise InfrahubError(f"GET /api/schema/{kind}: HTTP {status}: {str(body)[:300]}")
        return sorted(a["name"] for a in body.get("attributes", []))

    def kind_choices(self, kind=DEVICE_KIND, branch="main"):
        """``{attribute: [choice names]}`` for the kind's Dropdown attributes, as one API worker serves it."""
        status, body = self.request("GET", f"/api/schema/{kind}?branch={branch}", timeout=30)
        if status != 200 or not isinstance(body, dict):
            raise InfrahubError(f"GET /api/schema/{kind}: HTTP {status}: {str(body)[:300]}")
        return {
            a["name"]: sorted(c["name"] for c in (a.get("choices") or []))
            for a in body.get("attributes", [])
            if a.get("kind") == "Dropdown"
        }

    def choice_views(self, samples=24, kind=DEVICE_KIND):
        """Count the Dropdown choice sets served for ``kind`` over fresh connections."""
        views = {}
        for _ in range(samples):
            key = json.dumps(self.kind_choices(kind), sort_keys=True)
            views[key] = views.get(key, 0) + 1
        return views

    def dropdown_add(self, attribute, choice, kind=DEVICE_KIND, label=None, timeout=120):
        query = (
            "mutation($k: String!, $a: String!, $d: String!, $l: String) { SchemaDropdownAdd(data: "
            "{kind: $k, attribute: $a, dropdown: $d, label: $l}) { ok object { value } } }"
        )
        variables = {"k": kind, "a": attribute, "d": choice, "l": label}
        return self.graphql(query, variables, timeout=timeout)["SchemaDropdownAdd"]

    def dropdown_add_concurrently(self, changes, gap):
        """Send each ``(attribute, choice, label)`` change ``gap`` seconds after the previous one."""
        results = [None] * len(changes)

        def send(index, attribute, choice, label):
            started = time.time()
            try:
                answer = Infrahub(self.base, self.token, self.timeout).dropdown_add(attribute, choice, label=label)
                results[index] = {"ok": bool(answer.get("ok")), "answer": answer}
            except Exception as exc:
                results[index] = {"ok": False, "error": str(exc)[:300]}
            results[index].update(
                attribute=attribute, choice=choice, started=round(started, 3), seconds=round(time.time() - started, 2)
            )

        threads = []
        for index, (attribute, choice, label) in enumerate(changes):
            if index:
                time.sleep(gap)
            thread = threading.Thread(target=send, args=(index, attribute, choice, label))
            thread.start()
            threads.append(thread)
        for thread in threads:
            thread.join()
        return results

    def attribute_views(self, samples=24, kind=DEVICE_KIND):
        """Count the attribute lists served for ``kind`` over fresh connections (they spread over the API workers)."""
        views = {}
        for _ in range(samples):
            key = ",".join(self.kind_attributes(kind))
            views[key] = views.get(key, 0) + 1
        return views

    # ---------------------------------------------------------------- objects
    def create(self, kind, name, fields, branch="main"):
        data = ", ".join(
            [f"name: {{value: {json.dumps(name)}}}"] + [f"{k}: {{value: {json.dumps(v)}}}" for k, v in fields.items()]
        )
        result = self.graphql(f"mutation {{ {kind}Create(data: {{{data}}}) {{ ok object {{ id }} }} }}", branch=branch)
        return result[f"{kind}Create"]["object"]["id"]

    def create_device(self, name, fields, branch="main"):
        return self.create(DEVICE_KIND, name, fields, branch=branch)

    def create_devices(self, devices, branch="main", batch=50):
        """Create ``{name: fields}`` devices, ``batch`` aliased mutations per request."""
        items = list(devices.items())
        for start in range(0, len(items), batch):
            parts = []
            for i, (name, fields) in enumerate(items[start : start + batch]):
                data = ", ".join(
                    [f"name: {{value: {json.dumps(name)}}}"]
                    + [f"{k}: {{value: {json.dumps(v)}}}" for k, v in fields.items()]
                )
                parts.append(f"d{i}: {DEVICE_KIND}Create(data: {{{data}}}) {{ ok }}")
            self.graphql("mutation { " + " ".join(parts) + " }", branch=branch, timeout=300)

    def get(self, kind, name, fields, branch="main"):
        selection = " ".join(f"{f} {{ value }}" for f in fields)
        result = self.graphql(
            f"query($n: [String]) {{ {kind}(name__values: $n) {{ edges {{ node {{ id name {{ value }} "
            f"{selection} }} }} }} }}",
            {"n": [name]},
            branch=branch,
        )
        edges = result[kind]["edges"]
        if not edges:
            return None
        node = edges[0]["node"]
        return {"id": node["id"], **{f: (node.get(f) or {}).get("value") for f in ("name", *fields)}}

    def get_device(self, name, fields=("role", "site"), branch="main"):
        return self.get(DEVICE_KIND, name, fields, branch=branch)

    def count(self, kind=DEVICE_KIND, branch="main"):
        return self.graphql(f"{{ {kind} {{ count }} }}", branch=branch)[kind]["count"]

    def missing(self, names, kind=DEVICE_KIND, branch="main", chunk=200):
        """Names from ``names`` that do not exist on ``branch``."""
        names = list(names)
        missing = []
        for start in range(0, len(names), chunk):
            part = names[start : start + chunk]
            result = self.graphql(
                f"query($n: [String]) {{ {kind}(name__values: $n, limit: {len(part)}) "
                "{ edges { node { name { value } } } } }",
                {"n": part},
                branch=branch,
            )
            found = {e["node"]["name"]["value"] for e in result[kind]["edges"]}
            missing += [n for n in part if n not in found]
        return missing

    def missing_devices(self, names, branch="main", chunk=200):
        return self.missing(names, DEVICE_KIND, branch, chunk)

    def names(self, kind=DEVICE_KIND, branch="main", limit=5000):
        result = self.graphql(
            f"{{ {kind}(limit: {limit}) {{ edges {{ node {{ name {{ value }} }} }} }} }}", branch=branch
        )
        return {e["node"]["name"]["value"] for e in result[kind]["edges"]}

    # ---------------------------------------------------------------- branches
    def branch_create(self, name, timeout=300):
        query = "mutation($n: String!) { BranchCreate(data: {name: $n, sync_with_git: false}) { ok } }"
        return self.graphql(query, {"n": name}, timeout=timeout)["BranchCreate"]["ok"]

    def branch_status(self, name):
        result = self.graphql("query($n: String) { Branch(name: $n) { name status } }", {"n": name})
        branches = result.get("Branch") or []
        return branches[0]["status"] if branches else None

    def branches(self):
        return {b["name"]: b["status"] for b in self.graphql("{ Branch { name status } }").get("Branch") or []}

    def merge_submit(self, name):
        query = (
            "mutation($n: String!) { BranchMerge(data: {name: $n}, wait_until_completion: false) { ok task { id } } }"
        )
        return self.graphql(query, {"n": name}, timeout=120)["BranchMerge"]

    def task_state(self, task_id):
        result = self.graphql(
            "query($i: [String]) { InfrahubTask(ids: $i) { edges { node { id state } } } }", {"i": [task_id]}
        )
        edges = result["InfrahubTask"]["edges"]
        return edges[0]["node"]["state"] if edges else None

    def wait_merged(self, name, timeout, task_id=None):
        """Poll until the branch is MERGED: ``(seconds, None)``, or ``(None, reason)`` on failure or timeout."""
        start = time.monotonic()
        state = None
        while time.monotonic() - start < timeout:
            try:
                if self.branch_status(name) == "MERGED":
                    return round(time.monotonic() - start, 1), None
                if task_id:
                    state = self.task_state(task_id)
                    if state in ("FAILED", "CRASHED", "CANCELLED"):
                        return None, f"merge task {state}"
            except Exception:
                pass
            time.sleep(2)
        return None, f"not merged after {timeout}s (task {state})"
