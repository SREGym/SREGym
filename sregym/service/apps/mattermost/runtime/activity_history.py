"""Scaling activity recorded the way a cloud autoscaling group records it.

Shipped composed with ``capacity_scaler`` and ``fleet_scaler``: decisions are
the same, but only changes to desired capacity are recorded, each as an
activity naming the policy that caused it. Holds are not recorded, and readings
are not published next to each other: the dashboard shows the group, not the
metrics.
"""
# ruff: noqa: F821 -- api, record, latest, policy, gateway_metrics, readings_from, decide, fleet,
# ScaleInStabilizer, CONTROL, NAMESPACE, TARGET and INTERVAL come from the scalers, composed in.

import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread


def stamp(now):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))


def activity(current, target, kind, rules):
    """The activity record for one change of desired capacity, or None when nothing changed."""
    if target == current:
        return None
    count = abs(target - current)
    verb = (
        f"Launching {count} new instance{'s' * (count != 1)}"
        if target > current
        else (f"Terminating {count} instance{'s' * (count != 1)}")
    )
    change = f"changing the desired capacity from {current} to {target}"
    if kind == "scale_out":
        metric = rules["scale_out"]["metric"]
        cause = f"a monitor alarm {TARGET}-{metric}-high in state ALARM triggered policy {TARGET}-scale-out {change}"
    elif kind == "scale_in":
        metric = rules["scale_in"]["metric"]
        cause = f"a monitor alarm {TARGET}-{metric}-low in state ALARM triggered policy {TARGET}-scale-in {change}"
    else:
        cause = f"the group's size limits ({rules['min']}-{rules['max']}) took effect, {change}"
    return {
        "activity": verb,
        "cause": f"At {stamp(time.time())} {cause}.",
        "status": "Successful",
        "desired_capacity": target,
    }


def reconcile(stabilizer):
    rules = policy()
    scale_path = f"/apis/apps/v1/namespaces/{NAMESPACE}/deployments/{TARGET}/scale"
    scale = api(scale_path)
    current = scale["spec"].get("replicas", 1)
    fleet["requested"] = current
    if not rules["enabled"]:
        stabilizer.reset()
        return
    samples = gateway_metrics()
    readings = readings_from(samples)
    fleet.update(in_service=len(samples), readings=readings)
    now = time.monotonic()
    requested, _, kind = decide(current, readings, rules, now, fleet["last_scale_out"])
    target = requested
    if kind == "scale_in":
        target = stabilizer.target(current, requested, [s["pod_uid"] for s in samples], rules)
    else:
        stabilizer.reset()
    if target != current:
        api(
            scale_path,
            "PATCH",
            {"metadata": {"resourceVersion": scale["metadata"]["resourceVersion"]}, "spec": {"replicas": target}},
        )
        fleet["requested"] = target
    if kind == "scale_out":
        fleet["last_scale_out"] = now
    if entry := activity(current, target, kind, rules):
        record(entry)


def loop():
    stabilizer = ScaleInStabilizer()
    while True:
        try:
            reconcile(stabilizer)
        except Exception as exc:
            stabilizer.reset()
            record(
                {
                    "activity": "Updating group capacity",
                    "cause": f"At {stamp(time.time())} a scheduled evaluation could not complete.",
                    "status": "Failed",
                    "description": f"{type(exc).__name__}: {exc}",
                }
            )
        time.sleep(INTERVAL)


class Dashboard(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        return

    def do_GET(self):
        if self.path in ("/dashboard", "/health"):
            code, body = (
                200,
                {
                    "group": TARGET,
                    "desired_capacity": fleet["requested"],
                    "instances_in_service": fleet["in_service"],
                    "last_activity": {k: latest.get(k) for k in ("activity", "cause", "status")},
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
    print(f"capacity scaler group={TARGET} namespace={NAMESPACE} interval={INTERVAL}s", flush=True)
    Thread(target=loop, daemon=True).start()
    ThreadingHTTPServer(("", 8080), Dashboard).serve_forever()
