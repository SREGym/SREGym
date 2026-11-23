"""The capacity scaler over several groups, each with its own two policies.

Shipped composed with ``capacity_scaler``, ``fleet_scaler`` and
``activity_history``: decisions per group are those of ``fleet_scaler`` and are
recorded as ``activity_history`` records them. CPU comes from the cluster's
metrics API, as a horizontal autoscaler reads it, in percent of one core.
Saturation comes from each group's own telemetry: for a reverse proxy, requests
in progress against its connection limit; for a push relay, the average number
of sends in flight (time spent sending per second of wall time, from its
request-duration histogram) against a per-instance budget.
"""
# ruff: noqa: F821 -- api, record, latest, decide, in_service, observe, stamp, DEFAULT_POLICY, ScaleInStabilizer,
# CONTROL, NAMESPACE, INTERVAL and urllib come from the scalers, composed in.

import json
import os
import re
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

#: [{"name", "kind": "proxy"|"push", "port", "limit"}]; the first group's policy is scaler.json.
GROUPS = json.loads(os.environ.get("SCALER_GROUPS", "[]"))
WRITING = re.compile(r"Writing:\s*(\d+)")
DURATION_SUM = re.compile(r"^service_fcm_request_duration_seconds_sum(?:\{[^}]*\})?\s+([0-9.eE+-]+)$", re.M)

fleet = {g["name"]: {"in_service": None, "requested": None, "readings": {}, "last_scale_out": None} for g in GROUPS}
previous_sums = {}


def policy_path(group):
    return CONTROL / ("scaler.json" if group["name"] == GROUPS[0]["name"] else f"scaler-{group['name']}.json")


def policy(group):
    try:
        loaded = json.loads(policy_path(group).read_text())
    except (OSError, ValueError):
        return json.loads(json.dumps(DEFAULT_POLICY))
    merged = {**DEFAULT_POLICY, **loaded}
    for name in ("scale_in", "scale_out"):
        merged[name] = {**DEFAULT_POLICY[name], **(loaded.get(name) or {})}
    return merged


def cpu_by_pod(group):
    """Percent of one core per pod, from the metrics API."""
    usage = api(f"/apis/metrics.k8s.io/v1beta1/namespaces/{NAMESPACE}/pods?labelSelector=app%3D{group['name']}")
    result = {}
    for item in usage.get("items", []):
        total = 0.0
        for container in item.get("containers", []):
            total += cores(container["usage"]["cpu"])
        result[item["metadata"]["name"]] = round(100.0 * total, 2)
    return result


def cores(quantity):
    units = {"n": 1e-9, "u": 1e-6, "m": 1e-3}
    if quantity[-1] in units:
        return float(quantity[:-1]) * units[quantity[-1]]
    return float(quantity)


def proxy_saturation(text, limit):
    """Requests in progress (not counting this status request) against the connection limit."""
    match = WRITING.search(text)
    if not match:
        return None
    return round(100.0 * max(0, int(match.group(1)) - 1) / limit, 2)


def push_saturation(text, uid, limit, now):
    """Average sends in flight since the last scrape of this pod, against its budget."""
    match = DURATION_SUM.search(text)
    if not match:
        return None
    total = float(match.group(1))
    before = previous_sums.get(uid)
    previous_sums[uid] = (now, total)
    if before is None or now <= before[0]:
        return None
    in_flight = max(0.0, total - before[1]) / (now - before[0])
    return round(100.0 * in_flight / limit, 2)


def scrape(group, pod):
    path = "/nginx_status" if group["kind"] == "proxy" else "/metrics"
    with urllib.request.urlopen(f"http://{pod['status']['podIP']}:{group['port']}{path}", timeout=5) as response:
        return response.read().decode()


def gateway_metrics(group):
    pods = in_service(api(f"/api/v1/namespaces/{NAMESPACE}/pods?labelSelector=app%3D{group['name']}")["items"])
    cpu = cpu_by_pod(group)
    samples, now = [], time.monotonic()
    for pod in pods:
        uid = pod["metadata"]["uid"]
        try:
            text = scrape(group, pod)
        except (OSError, ValueError, TypeError):
            continue
        if group["kind"] == "proxy":
            saturation = proxy_saturation(text, group["limit"])
        else:
            saturation = push_saturation(text, uid, group["limit"], now)
        sample = {"pod_uid": uid}
        if pod["metadata"]["name"] in cpu:
            sample["cpu_percent"] = cpu[pod["metadata"]["name"]]
        if saturation is not None:
            sample["saturation_percent"] = saturation
        samples.append(sample)
    return samples


def activity(name, current, target, kind, rules):
    if target == current:
        return None
    count = abs(target - current)
    verb = (
        f"Launching {count} new instance{'s' * (count != 1)}"
        if target > current
        else f"Terminating {count} instance{'s' * (count != 1)}"
    )
    change = f"changing the desired capacity from {current} to {target}"
    if kind in ("scale_out", "scale_in"):
        side = "out" if kind == "scale_out" else "in"
        metric = rules[kind]["metric"]
        level = "high" if kind == "scale_out" else "low"
        cause = f"a monitor alarm {name}-{metric}-{level} in state ALARM triggered policy {name}-scale-{side} {change}"
    else:
        cause = f"the group's size limits ({rules['min']}-{rules['max']}) took effect, {change}"
    return {
        "group": name,
        "activity": verb,
        "cause": f"At {stamp(time.time())} {cause}.",
        "status": "Successful",
        "desired_capacity": target,
    }


def readings_from(samples):
    return {
        "cpu": observe_present(samples, "cpu_percent"),
        "saturation": observe_present(samples, "saturation_percent"),
    }


def observe_present(samples, field):
    """The mean over the pods that reported it; None when none did."""
    present = [s for s in samples if field in s]
    return observe(present, field) if present else None


def reconcile(group, stabilizer):
    name, state = group["name"], fleet[group["name"]]
    rules = policy(group)
    scale_path = f"/apis/apps/v1/namespaces/{NAMESPACE}/deployments/{name}/scale"
    scale = api(scale_path)
    current = scale["spec"].get("replicas", 1)
    state["requested"] = current
    if not rules["enabled"]:
        stabilizer.reset()
        return
    samples = gateway_metrics(group)
    readings = readings_from(samples)
    state.update(in_service=len(samples), readings=readings)
    now = time.monotonic()
    requested, _, kind = decide(current, readings, rules, now, state["last_scale_out"])
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
        state["requested"] = target
    if kind == "scale_out":
        state["last_scale_out"] = now
    if entry := activity(name, current, target, kind, rules):
        record(entry)


def loop():
    stabilizers = {g["name"]: ScaleInStabilizer() for g in GROUPS}
    while True:
        for group in GROUPS:
            try:
                reconcile(group, stabilizers[group["name"]])
            except Exception as exc:
                stabilizers[group["name"]].reset()
                record(
                    {
                        "group": group["name"],
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
            body = {
                "groups": [
                    {
                        "group": name,
                        "desired_capacity": state["requested"],
                        "instances_in_service": state["in_service"],
                    }
                    for name, state in fleet.items()
                ],
                "last_activity": {k: latest.get(k) for k in ("group", "activity", "cause", "status")},
            }
            code = 200
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
    names = ",".join(g["name"] for g in GROUPS)
    print(f"capacity scaler fleet={names} namespace={NAMESPACE} interval={INTERVAL}s", flush=True)
    Thread(target=loop, daemon=True).start()
    ThreadingHTTPServer(("", 8080), Dashboard).serve_forever()
