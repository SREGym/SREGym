import json
import os
import socket
import threading
import time
import urllib.error
import urllib.request

BASE = os.environ.get("INFRAHUB_ADDRESS", "http://infrahub-server:8000")
TOKEN = os.environ["INFRAHUB_API_TOKEN"]
CONFIG = os.environ.get("CONFIG_FILE", "/etc/inventory-sync/fields.json")
INTERVAL = float(os.environ.get("SYNC_INTERVAL", "3"))
CHANGE_INTERVAL = int(os.environ.get("CHANGE_INTERVAL", "0"))
MERGE_TIMEOUT = int(os.environ.get("MERGE_TIMEOUT", "600"))
CREATE_TIMEOUT = int(os.environ.get("CREATE_TIMEOUT", "120"))
REVIEW_SLOTS = int(os.environ.get("REVIEW_SLOTS", "3"))
PAUSE_DURING_MERGE = os.environ.get("PAUSE_DURING_MERGE", "1") == "1"
BUSY_BACKOFF = 30
RELEASE_CHECK = 60
HOST = socket.gethostname().rsplit("-", 1)[-1]


class ApiError(RuntimeError):
    pass


def log(**event):
    print(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **event}), flush=True)


def graphql(query, variables=None, branch="main", timeout=60):
    body = json.dumps({"query": query, "variables": variables or {}}).encode()
    headers = {"X-INFRAHUB-KEY": TOKEN, "Content-Type": "application/json"}
    request = urllib.request.Request(f"{BASE}/graphql/{branch}", data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        raise ApiError(f"HTTP {exc.code}: {exc.read()[:300]!r}") from None
    if payload.get("errors"):
        raise ApiError("; ".join(str(e.get("message")) for e in payload["errors"]))
    return payload.get("data") or {}


def create(kind, name, fields, branch="main"):
    data = ", ".join(f"{k}: {{value: {json.dumps(v)}}}" for k, v in {"name": name, **fields}.items())
    graphql(f"mutation {{ {kind}Create(data: {{{data}}}) {{ ok }} }}", branch=branch)


def read(kind, name, fields):
    selection = " ".join(f"{f} {{ value }}" for f in fields)
    result = graphql(
        f"query($n: [String]) {{ {kind}(name__values: $n) {{ edges {{ node {{ {selection} }} }} }} }}", {"n": [name]}
    )
    edges = result[kind]["edges"]
    return {f: (edges[0]["node"].get(f) or {}).get("value") for f in fields} if edges else None


def branches():
    return {b["name"]: b["status"] for b in graphql("{ Branch { name status } }").get("Branch") or []}


def branch_status(name):
    found = graphql("query($n: String) { Branch(name: $n) { status } }", {"n": name}).get("Branch") or []
    return found[0]["status"] if found else None


def task_state(task_id):
    edges = graphql("query($i: [String]) { InfrahubTask(ids: $i) { edges { node { state } } } }", {"i": [task_id]})[
        "InfrahubTask"
    ]["edges"]
    return edges[0]["node"]["state"] if edges else None


def merge(branch):
    result = graphql(
        "mutation($n: String!) { BranchMerge(data: {name: $n}, wait_until_completion: false) { ok task { id } } }",
        {"n": branch},
        timeout=120,
    )["BranchMerge"]
    task = (result.get("task") or {}).get("id")
    deadline = time.monotonic() + MERGE_TIMEOUT
    state = None
    while time.monotonic() < deadline:
        try:
            if branch_status(branch) == "MERGED":
                return None
            if task:
                state = task_state(task)
                if state in ("FAILED", "CRASHED", "CANCELLED"):
                    return f"merge task {state}"
        except Exception:
            pass
        time.sleep(2)
    return f"not merged after {MERGE_TIMEOUT}s (task {state})"


def config():
    try:
        with open(CONFIG) as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {}


def value(field, name, n, choices):
    if choices.get(field):
        return choices[field][n % len(choices[field])]
    return {"role": "leaf", "site": f"dc{n % 3 + 1}", "device": name, "slot": str(n % 4 + 1)}.get(
        field, f"{field}-{HOST}-{n}"
    )


def register(kind, name, wanted, n, parent=None, choices=None):
    values = {f: value(f, parent or name, n, choices or {}) for f in wanted}
    try:
        create(kind, name, values)
        seen = read(kind, name, wanted)
        if seen != values:
            raise ApiError(f"read back {seen} instead of {values}")
        log(op="register", kind=kind, ok=True, name=name)
        return True
    except Exception as exc:
        message = str(exc)[:400]
        log(op="register", kind=kind, ok=False, name=name, error=message)
        if "merge is currently in progress" in message:
            time.sleep(BUSY_BACKOFF)
        return False


def sync_loop():
    n = 0
    while True:
        n += 1
        settings = config()
        device_fields = settings.get("NetworkDevice", ["role", "site"])
        module_fields = settings.get("NetworkModule", ["device", "slot"])
        choices = settings.get("values", {})
        name = f"inv-{HOST}-{int(time.time())}-{n}"
        if PAUSE_DURING_MERGE:
            try:
                if any(status == "MERGING" for status in branches().values()):
                    time.sleep(INTERVAL)
                    continue
            except Exception as exc:
                log(op="list_branches", ok=False, error=str(exc)[:400])
        register("NetworkDevice", name, device_fields, n, choices=choices)
        register("NetworkModule", f"{name}-m1", module_fields, n, parent=name, choices=choices)
        time.sleep(INTERVAL)


def change_loop():
    try:
        backlog = sorted(b for b, s in branches().items() if b.startswith("change-") and s == "OPEN")
    except Exception:
        backlog = []
    while True:
        slot = (int(time.time()) // CHANGE_INTERVAL + 1) * CHANGE_INTERVAL
        time.sleep(max(0.0, slot - time.time()))
        due = [b for b in backlog if int(b.split("-")[1]) <= slot - REVIEW_SLOTS * CHANGE_INTERVAL]
        if due:
            branch = due[0]
            backlog.remove(branch)
            started = time.monotonic()
            try:
                error = merge(branch)
                if error:
                    log(op="merge", ok=False, branch=branch, status=branch_status(branch), error=error)
                else:
                    log(op="merge", ok=True, branch=branch, seconds=round(time.monotonic() - started, 1))
            except Exception as exc:
                log(op="merge", ok=False, branch=branch, error=str(exc)[:400])
        branch = f"change-{slot}"
        try:
            graphql(
                "mutation($n: String!) { BranchCreate(data: {name: $n, sync_with_git: false}) { ok } }",
                {"n": branch},
                timeout=CREATE_TIMEOUT,
            )
            create("NetworkDevice", f"dev-{branch}", {"role": "spine", "site": "dc1"}, branch=branch)
            backlog.append(branch)
            log(op="prepare_change", ok=True, branch=branch)
        except Exception as exc:
            log(op="prepare_change", ok=False, branch=branch, error=str(exc)[:400])


def release_loop():
    """Keep every branch the release plan lists merged into main: merge it again while it is OPEN."""
    while True:
        time.sleep(RELEASE_CHECK)
        for branch in config().get("releases", []):
            try:
                status = branch_status(branch)
            except Exception as exc:
                log(op="release", ok=False, branch=branch, error=str(exc)[:400])
                continue
            if status != "OPEN":
                continue
            started = time.monotonic()
            try:
                error = merge(branch)
            except Exception as exc:
                error = str(exc)[:400]
            if error:
                log(op="release", ok=False, branch=branch, status=branch_status(branch), error=error)
            else:
                log(op="release", ok=True, branch=branch, seconds=round(time.monotonic() - started, 1))


def main():
    log(op="start", change_interval=CHANGE_INTERVAL)
    if CHANGE_INTERVAL > 0:
        threading.Thread(target=change_loop, daemon=True).start()
    threading.Thread(target=release_loop, daemon=True).start()
    sync_loop()


if __name__ == "__main__":
    main()
