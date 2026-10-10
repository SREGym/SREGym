"""Capacity automation keyed on CPU, which shrinks the gateway during the incident.

The policy is ordinary and was reasonable when written: scale out on high CPU,
scale in on low CPU. It is also exactly wrong here, because the gateway's CPU
falls when it saturates. The automation therefore removes capacity from a service
that is already failing, and reverts a responder's manual scale-up.

The policy is a control file, so this is repairable as well as stoppable: disable
it, raise its floor to the service's capacity floor, or key it on saturation.
"""

import json
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


def gateway_metrics():
    """Read every gateway pod directly. The Service would only reach one."""
    pods = api(f"/api/v1/namespaces/{NAMESPACE}/pods?labelSelector=app%3D{TARGET}")["items"]
    samples = []
    for pod in pods:
        address = pod.get("status", {}).get("podIP")
        phase = pod.get("status", {}).get("phase")
        if not address or phase != "Running" or pod["metadata"].get("deletionTimestamp"):
            continue
        try:
            with urllib.request.urlopen(f"http://{address}:8080/metrics", timeout=5) as response:
                samples.append(json.loads(response.read()))
        except Exception:
            continue
    return samples


def observe(samples, metric):
    values = [s[metric] for s in samples if metric in s]
    if not values:
        return None
    return round(sum(values) / len(values), 2)


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


def loop():
    while True:
        try:
            rules = policy()
            metric = "cpu_percent" if rules["metric"] == "cpu" else "saturation_percent"
            scale = api(f"/apis/apps/v1/namespaces/{NAMESPACE}/deployments/{TARGET}/scale")
            current = scale["spec"].get("replicas", 1)
            if not rules["enabled"]:
                record({"observed": None, "replicas": current, "decision": "disabled", "metric": rules["metric"]})
            else:
                observed = observe(gateway_metrics(), metric)
                target, why = decide(current, observed, rules, rules["metric"])
                if target != current:
                    api(
                        f"/apis/apps/v1/namespaces/{NAMESPACE}/deployments/{TARGET}/scale",
                        "PATCH",
                        {"spec": {"replicas": target}},
                    )
                record(
                    {
                        "observed": observed,
                        "replicas": target,
                        "decision": f"{current} -> {target}: {why}",
                        "metric": rules["metric"],
                    }
                )
        except Exception as exc:
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
