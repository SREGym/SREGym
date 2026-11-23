"""Two independent scaling policies over the chat-gateway fleet.

Shipped composed with ``capacity_scaler``: its API client, decision log and
scale-in stabilizer are kept, and every definition here replaces the
single-metric one of the same name (see ``transit_cascade.composed_source``). As in a cloud autoscaling
group, a scale-out policy and a scale-in policy each watch their own metric and
act on their own -- scale-in tracks its metric's target, removing as many
instances as the reading says are spare -- and instances only count once they are in service: a pod that
has not passed provisioning is capacity requested, not capacity available.
"""
# ruff: noqa: F821 -- api, observe, record, latest, loop, POLICY, CONTROL, NAMESPACE,
# TARGET, INTERVAL and urllib come from capacity_scaler, composed in.

import json
import math
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

METRICS = {"cpu": "cpu_percent", "saturation": "saturation_percent"}
#: Fail safe, as before: no policy file means change nothing.
DEFAULT_POLICY = {
    "enabled": False,
    "min": 1,
    "max": 12,
    "scale_in": {"enabled": True, "metric": "cpu", "below": 25},
    "scale_out": {"enabled": True, "metric": "saturation", "above": 75, "step": 3, "cooldown_seconds": 60},
}

fleet = {"in_service": None, "requested": None, "readings": {}, "last_scale_out": None}


def policy():
    try:
        loaded = json.loads(POLICY.read_text())
    except (OSError, ValueError):
        return json.loads(json.dumps(DEFAULT_POLICY))
    merged = {**DEFAULT_POLICY, **loaded}
    for name in ("scale_in", "scale_out"):
        merged[name] = {**DEFAULT_POLICY[name], **(loaded.get(name) or {})}
    return merged


def in_service(pods):
    """Pods that are running, ready and not being deleted."""
    ready = []
    for pod in pods:
        status = pod.get("status", {})
        if pod["metadata"].get("deletionTimestamp") or status.get("phase") != "Running" or not status.get("podIP"):
            continue
        if any(c.get("type") == "Ready" and c.get("status") == "True" for c in status.get("conditions", [])):
            ready.append(pod)
    return ready


def gateway_metrics():
    """Scrape every in-service gateway; the reading is over those that answered."""
    pods = api(f"/api/v1/namespaces/{NAMESPACE}/pods?labelSelector=app%3D{TARGET}")["items"]
    samples = []
    for pod in in_service(pods):
        try:
            with urllib.request.urlopen(f"http://{pod['status']['podIP']}:8080/metrics", timeout=5) as response:
                samples.append({**json.loads(response.read()), "pod_uid": pod["metadata"]["uid"]})
        except (OSError, ValueError, TypeError):
            continue
    return samples


def readings_from(samples):
    return {name: observe(samples, field) for name, field in METRICS.items()}


def decide(current, readings, rules, now, last_scale_out):
    """Return ``(target, why, kind)``; kind is scale_out, scale_in, bound or hold."""
    low, high = rules["min"], rules["max"]
    if current < low:
        return low, f"{current} below policy min {low}", "bound"
    if current > high:
        return high, f"{current} above policy max {high}", "bound"
    out, inward = rules["scale_out"], rules["scale_in"]
    notes = []
    if out.get("enabled", True):
        value = readings.get(out["metric"])
        if value is not None and value > out["above"]:
            if current >= high:
                notes.append(f"{out['metric']} {value}% above {out['above']}% but at max {high}")
            elif last_scale_out is not None and now - last_scale_out < out["cooldown_seconds"]:
                notes.append(f"{out['metric']} {value}% above {out['above']}%, scale-out cooling down")
            else:
                added = max(out["step"], math.ceil(current * out.get("percent", 0) / 100))
                return min(high, current + added), f"{out['metric']} {value}% above {out['above']}%", "scale_out"
    if inward.get("enabled", True):
        value = readings.get(inward["metric"])
        if value is not None and value < inward["below"]:
            if current > low:
                tracked = math.ceil(current * value / inward["below"])
                return (
                    max(low, min(current - 1, tracked)),
                    "; ".join([*notes, f"{inward['metric']} {value}% below {inward['below']}%"]),
                    "scale_in",
                )
            notes.append(f"{inward['metric']} {value}% below {inward['below']}% but at min {low}")
    if not any(value is not None for value in readings.values()):
        notes.append("no metric available")
    return current, "; ".join(notes) or "within band", "hold"


def reconcile(stabilizer):
    rules = policy()
    scale_path = f"/apis/apps/v1/namespaces/{NAMESPACE}/deployments/{TARGET}/scale"
    scale = api(scale_path)
    current = scale["spec"].get("replicas", 1)
    fleet["requested"] = current
    if not rules["enabled"]:
        stabilizer.reset()
        record({"observed": None, "replicas": current, "decision": "disabled", "metric": "fleet"})
        return
    samples = gateway_metrics()
    readings = readings_from(samples)
    fleet.update(in_service=len(samples), readings=readings)
    now = time.monotonic()
    requested, why, kind = decide(current, readings, rules, now, fleet["last_scale_out"])
    target = requested
    if kind == "scale_in":
        target = stabilizer.target(current, requested, [s["pod_uid"] for s in samples], rules)
        if target != requested:
            why += "; waiting for stable measurements"
    else:
        stabilizer.reset()
    if target != current:
        api(
            scale_path,
            "PATCH",
            {"metadata": {"resourceVersion": scale["metadata"]["resourceVersion"]}, "spec": {"replicas": target}},
        )
    if kind == "scale_out":
        fleet["last_scale_out"] = now
    record(
        {
            "observed": readings,
            "in_service": len(samples),
            "replicas": target,
            "decision": f"{current} -> {target}: {why}",
            "metric": "fleet",
        }
    )


class Dashboard(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        return

    def do_GET(self):
        if self.path in ("/dashboard", "/health"):
            code, body = (
                200,
                {
                    "gateway_replicas_requested": fleet["requested"],
                    "gateway_replicas_in_service": fleet["in_service"],
                    "cpu_utilization_percent": fleet["readings"].get("cpu"),
                    "worker_saturation_percent": fleet["readings"].get("saturation"),
                    "last_capacity_decision": latest["decision"],
                },
            )
        else:
            code, body = 404, {"error": "not found"}
        payload = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


if __name__ == "__main__":
    CONTROL.mkdir(parents=True, exist_ok=True)
    print(f"fleet scaler target={TARGET} namespace={NAMESPACE} interval={INTERVAL}s", flush=True)
    Thread(target=loop, daemon=True).start()
    ThreadingHTTPServer(("", 8080), Dashboard).serve_forever()
