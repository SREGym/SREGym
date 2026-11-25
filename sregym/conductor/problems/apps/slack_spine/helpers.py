"""Slack Spine: helpers."""

from __future__ import annotations

import contextlib
import json
import shlex
import time

from kubernetes import client
from kubernetes.client.rest import ApiException

from sregym.service.rollout import deployment_rollout_complete

APP = "slack_spine"


def selector_of(problem, deployment: str) -> str:
    """Label selector built from a Deployment's ``spec.selector.matchLabels``."""
    dep = problem.kubectl.get_deployment(deployment, problem.namespace)
    return ",".join(f"{k}={v}" for k, v in sorted(dep.spec.selector.match_labels.items()))


def delete_pods(problem, deployment: str) -> list[str]:
    """Delete every pod of ``deployment`` so its ReplicaSet must create new ones under the fault."""
    pods = problem.kubectl.core_v1_api.list_namespaced_pod(
        problem.namespace, label_selector=selector_of(problem, deployment)
    ).items
    names = [pod.metadata.name for pod in pods]
    for name in names:
        try:
            problem.kubectl.core_v1_api.delete_namespaced_pod(
                name, problem.namespace, body=client.V1DeleteOptions(grace_period_seconds=0)
            )
        except ApiException as exc:
            if exc.status != 404:
                raise
    print(f"Deleted {deployment} pods {names}")
    return names


def wait_rollout(problem, deployment: str, timeout_s: int = 600, check: bool = False) -> bool:
    """Wait until ``deployment`` is fully rolled out and Ready; returns whether it did."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            if deployment_rollout_complete(problem.kubectl.get_deployment(deployment, problem.namespace)):
                return True
        except ApiException:
            pass
        time.sleep(5)
    if check:
        raise RuntimeError(f"deployment {deployment} did not become Ready within {timeout_s}s")
    print(f"[warn] deployment {deployment} not Ready after {timeout_s}s")
    return False


_FETCH_PARALLEL = """
import json, sys, time, urllib.error, urllib.request
from concurrent.futures import ThreadPoolExecutor
def one(item):
    key, url, method, body = item
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    start = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return key, [resp.status, resp.read().decode("utf-8", "replace"), time.monotonic() - start]
    except urllib.error.HTTPError as exc:
        return key, [exc.code, exc.read().decode("utf-8", "replace"), time.monotonic() - start]
    except Exception as exc:
        return key, [0, repr(exc), time.monotonic() - start]
items = json.load(sys.stdin)
with ThreadPoolExecutor(max_workers=max(1, min(32, len(items)))) as pool:
    print(json.dumps(dict(pool.map(one, items))))
"""


def fetch_parallel(problem, requests: list[tuple[str, str, str, object]], timeout: float = 120) -> dict:
    """Issue HTTP requests concurrently from the ops toolbox; {key: [status, body, seconds]}."""
    out = problem.app.toolbox_exec(
        "python3 -c " + shlex.quote(_FETCH_PARALLEL),
        input_data=json.dumps([list(item) for item in requests]),
        timeout=timeout,
    )
    return json.loads(out.strip().splitlines()[-1])


def role_pod_ips(problem, role: str) -> dict[str, str]:
    pods = problem.kubectl.core_v1_api.list_namespaced_pod(
        problem.namespace, label_selector=problem.app.role_selector(role)
    ).items
    return {
        pod.metadata.name: pod.status.pod_ip
        for pod in pods
        if pod.metadata.deletion_timestamp is None and pod.status.phase == "Running" and pod.status.pod_ip
    }


_EXTRA_TRUSTED_PREFIXES = ('    "postgres:",', '    "redpandadata/",', '    "minio/",')


SEND_HEAVY_ACTION_WEIGHTS = {
    "session_login": 0.12,
    "session_history": 9.79,
    "session_unread": 1.32,
    "session_post": 6.0,
    "session_thread": 0.88,
    "session_reply": 0.18,
    "session_reaction": 0.13,
    "session_edit": 0.04,
    "session_delete": 0.02,
    "session_file_upload": 0.08,
    "session_file_download": 0.08,
    "session_presence": 0.144,
    "session_search": 0.054,
}


_SPIKE_SCRIPT = """
import asyncio, json, time
RPS, DUR = {rps}, {seconds}
TOTAL = RPS * DUR
sent = written = finished_writes = 0
async def send(i):
    global written, finished_writes
    body = json.dumps({{'channel_id': f'spike-{{i % 64}}', 'client_msg_id': f'spike-{{time.time_ns()}}-{{i}}', 'text': 'spike'}}).encode()
    head = ('POST /messages HTTP/1.1\\r\\nHost: svc-message\\r\\nContent-Type: application/json\\r\\n'
            f'Content-Length: {{len(body)}}\\r\\nConnection: close\\r\\n\\r\\n').encode()
    w = None
    try:
        try:
            r, w = await asyncio.wait_for(asyncio.open_connection('svc-message', 8000), 10)
            w.write(head + body)
            await asyncio.wait_for(w.drain(), 10)
            written += 1
        finally:
            finished_writes += 1
        await asyncio.wait_for(r.read(), 10)
    except Exception:
        pass
    finally:
        if w is not None:
            w.close()
async def main():
    global sent
    tasks = set()
    start = time.monotonic()
    for i in range(TOTAL):
        task = asyncio.ensure_future(send(i))
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        sent = i + 1
        await asyncio.sleep(max(0.0, start + sent / RPS - time.monotonic()))
    # Each connect/write has its own timeout. Do not cancel pending writes,
    # but do not wait for responses from the service we are overloading.
    while finished_writes < sent:
        await asyncio.sleep(0.05)
asyncio.run(main())
print('spike sent', written, 'of', sent)
if written != sent:
    raise SystemExit(f'{{sent - written}} requests were not written')
"""


DEFAULT_MESH = {
    "retries": 1,
    "retryOnTimeout": False,
    "perTryTimeoutMs": 3000,
    "backoffMs": 0,
    "breakerEnabled": False,
    "breakerThreshold": 1000000,
}


_FETCH_MANY = """
import json, sys, urllib.error, urllib.request
out = {}
for key, url, method, body in json.load(sys.stdin):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            out[key] = [resp.status, resp.read().decode("utf-8", "replace")]
    except urllib.error.HTTPError as exc:
        out[key] = [exc.code, exc.read().decode("utf-8", "replace")]
    except Exception as exc:
        out[key] = [0, repr(exc)]
print(json.dumps(out))
"""


def _prom_sum(text: str, name: str, **labels: str) -> float:
    """Sum every sample of ``name`` whose labels include ``labels``."""
    total = 0.0
    for line in text.splitlines():
        if not line.startswith(name) or line.startswith("#"):
            continue
        head, _, value = line.rpartition(" ")
        metric, _, label_text = head.partition("{")
        if metric != name:
            continue
        if any(f'{key}="{val}"' not in label_text for key, val in labels.items()):
            continue
        with contextlib.suppress(ValueError):
            total += float(value)
    return total
