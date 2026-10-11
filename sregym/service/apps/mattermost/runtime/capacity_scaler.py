"""Scale gateway replicas from measured usage and a persistent capacity policy."""

import json
import math
import os
import ssl
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

CONTROL = Path(os.environ.get("CONTROL_PATH", "/control"))
POLICY = CONTROL / "scaler.json"
DECISIONS = CONTROL / "scaler-decisions.jsonl"
NAMESPACE = os.environ.get("NAMESPACE", "mattermost")
TARGET = os.environ.get("SCALER_TARGET", "chat-gateway")
INTERVAL = float(os.environ.get("SCALER_INTERVAL", "15"))
SCALE_IN_STABILIZATION_SECONDS = 45

API = "https://kubernetes.default.svc"
SECRETS = Path("/var/run/secrets/kubernetes.io/serviceaccount")
#: Fail safe. A missing or unreadable policy file means "change nothing" rather
#: than "apply a guess": at startup the file may not be written yet, and acting
#: on an uncalibrated threshold would resize the service before anyone asked.
DEFAULT_POLICY = {"enabled": False, "metric": "cpu", "scale_in_below": 25, "scale_out_above": 60, "min": 1, "max": 6}

latest = {"observed": None, "replicas": None, "decision": "starting", "metric": "cpu"}


def token():
    return (SECRETS / "token").read_text().strip()


def api(path, method="GET", body=None):
    request = urllib.request.Request(
        API + path,
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={
            "Authorization": "Bearer " + token(),
            "Accept": "application/json",
            **({"Content-Type": "application/merge-patch+json"} if body is not None else {}),
        },
    )
    context = ssl.create_default_context(cafile=str(SECRETS / "ca.crt"))
    with urllib.request.urlopen(request, timeout=10, context=context) as response:
        return json.loads(response.read())


def policy():
    try:
        return {**DEFAULT_POLICY, **json.loads(POLICY.read_text())}
    except (OSError, ValueError):
        return dict(DEFAULT_POLICY)


def gateway_metrics(expected_replicas):
    """Read every gateway pod directly. The Service would only reach one."""
    pods = api(f"/api/v1/namespaces/{NAMESPACE}/pods?labelSelector=app%3D{TARGET}")["items"]
    pods = [
        pod
        for pod in pods
        if not pod["metadata"].get("deletionTimestamp")
        and pod.get("status", {}).get("phase") not in {"Failed", "Succeeded"}
    ]
    if len(pods) != expected_replicas:
        return []
    samples = []
    for pod in pods:
        status = pod.get("status", {})
        address = status.get("podIP")
        ready = any(c.get("type") == "Ready" and c.get("status") == "True" for c in status.get("conditions", []))
        if not address or status.get("phase") != "Running" or not ready:
            return []
        try:
            with urllib.request.urlopen(f"http://{address}:8080/metrics", timeout=5) as response:
                samples.append({**json.loads(response.read()), "pod_uid": pod["metadata"]["uid"]})
        except (OSError, ValueError, TypeError):
            return []
    return samples


def observe(samples, metric):
    """Missing measurements are unknown, not evidence of spare capacity."""
    values = [s.get(metric) for s in samples]
    if not values or any(type(value) not in (int, float) or not math.isfinite(value) or value < 0 for value in values):
        return None
    return round(sum(values) / len(values), 2)


class ScaleInStabilizer:
    """Require sustained low usage from the same ready pod population."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.context = None
        self.since = None

    def target(self, current, requested, pod_uids, rules):
        if requested >= current:
            self.reset()
            return requested
        context = (current, tuple(sorted(pod_uids)), dict(rules))
        now = time.monotonic()
        if context != self.context:
            self.context, self.since = context, now
        if now - self.since < SCALE_IN_STABILIZATION_SECONDS:
            return current
        return requested


def decide(current, observed, rules, metric):
    """Return the replica count this policy wants, and why."""
    if observed is None:
        return current, "no metric available"
    if metric == "cpu":
        # The incident's policy. Low CPU is read as spare capacity.
        if observed < rules["scale_in_below"]:
            return max(rules["min"], current - 1), f"cpu {observed}% below {rules['scale_in_below']}%"
        if observed > rules["scale_out_above"]:
            return min(rules["max"], current + 1), f"cpu {observed}% above {rules['scale_out_above']}%"
        return current, f"cpu {observed}% within band"
    # Saturation reads the incident the right way round: busy workers mean the
    # gateway needs more capacity, not less.
    if observed > rules["scale_out_above"]:
        return min(rules["max"], current + 1), f"saturation {observed}% above {rules['scale_out_above']}%"
    if observed < rules["scale_in_below"]:
        return max(rules["min"], current - 1), f"saturation {observed}% below {rules['scale_in_below']}%"
    return current, f"saturation {observed}% within band"


def record(entry):
    latest.update(entry)
    with DECISIONS.open("a") as stream:
        stream.write(json.dumps({"at": time.time(), **entry}) + "\n")


def reconcile(stabilizer):
    rules = policy()
    metric = "cpu_percent" if rules["metric"] == "cpu" else "saturation_percent"
    scale_path = f"/apis/apps/v1/namespaces/{NAMESPACE}/deployments/{TARGET}/scale"
    scale = api(scale_path)
    current = scale["spec"].get("replicas", 1)
    if not rules["enabled"]:
        stabilizer.reset()
        record({"observed": None, "replicas": current, "decision": "disabled", "metric": rules["metric"]})
        return
    samples = gateway_metrics(current)
    observed = observe(samples, metric)
    requested, why = decide(current, observed, rules, rules["metric"])
    target = stabilizer.target(current, requested, [s["pod_uid"] for s in samples], rules)
    if target != requested:
        why += "; waiting for stable measurements"
    if target != current:
        api(
            scale_path,
            "PATCH",
            {"metadata": {"resourceVersion": scale["metadata"]["resourceVersion"]}, "spec": {"replicas": target}},
        )
    record(
        {
            "observed": observed,
            "replicas": target,
            "decision": f"{current} -> {target}: {why}",
            "metric": rules["metric"],
        }
    )


def loop():
    stabilizer = ScaleInStabilizer()
    while True:
        try:
            reconcile(stabilizer)
        except Exception as exc:
            stabilizer.reset()
            record({"decision": f"error: {type(exc).__name__}: {exc}"})
        time.sleep(INTERVAL)


class Dashboard(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        return

    def do_GET(self):
        if self.path not in ("/dashboard", "/health"):
            # The capacity dashboard survived the incident; the latency and
            # saturation panels did not. They are still in the gateway's own
            # /metrics and logs.
            payload = json.dumps({"error": "panel unavailable"}).encode()
            self.send_response(404)
        else:
            payload = json.dumps(
                {
                    "panels": ["cpu_utilization", "gateway_replicas"],
                    "unavailable_panels": ["request_latency", "worker_saturation", "error_rate"],
                    "cpu_utilization_percent": latest["observed"] if latest["metric"] == "cpu" else None,
                    "gateway_replicas": latest["replicas"],
                    "last_capacity_decision": latest["decision"],
                }
            ).encode()
            self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


if __name__ == "__main__":
    CONTROL.mkdir(parents=True, exist_ok=True)
    print(f"capacity scaler target={TARGET} namespace={NAMESPACE} interval={INTERVAL}s", flush=True)
    Thread(target=loop, daemon=True).start()
    ThreadingHTTPServer(("", 8080), Dashboard).serve_forever()
