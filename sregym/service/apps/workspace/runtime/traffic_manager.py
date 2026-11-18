"""Traffic manager for the edge gateway fleet.

Every cycle it probes each gateway unit, ejects units that fail and readmits
units that recover, then decides which units serve each region. It applies the
decision by labelling unit pods `steer-<region>=in`; each region's `mail-<region>`
and `chat-<region>` Services select on those labels, or on the alternate gateway
when a service is placed there.

Policy is read from the `traffic-policy` ConfigMap on every cycle:

    probe                 admission (connect through the admission path) | liveness
    probe_interval        seconds between probe rounds
    probe_timeout         seconds a probe may take
    unhealthy_threshold   consecutive failures before a unit is ejected
    healthy_threshold     consecutive successes before an ejected unit returns
    max_ejection_percent  most units of a region that may be ejected at once;
                          beyond it the least-recently-failed stay in rotation
    spillover             regional | global: where a region's traffic may go
                          when too few of its own units are healthy
    spillover_below       healthy fraction under which a region spills
    anomaly_redirect      enabled | disabled: redirect a region's traffic to
                          other regions as soon as its probe failure rate
                          crosses anomaly_threshold, regardless of spillover
    anomaly_threshold     probe failure fraction that counts as an anomaly
    drained               units held out of rotation for maintenance
    placement.<service>   primary | alternate
"""

import json
import socket
import ssl
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SA = "/var/run/secrets/kubernetes.io/serviceaccount"
API = "https://kubernetes.default.svc"
SERVICES = ("mail", "chat")

DEFAULTS = {
    "probe": "admission",
    "probe_interval": "5",
    "probe_timeout": "2",
    "unhealthy_threshold": "2",
    "healthy_threshold": "3",
    "max_ejection_percent": "50",
    "spillover": "regional",
    "spillover_below": "0.5",
    "anomaly_redirect": "disabled",
    "anomaly_threshold": "0.3",
    "drained": "",
    "placement.mail": "primary",
    "placement.chat": "primary",
}


def parse_policy(text):
    """Parse `key = value` lines over the defaults; unknown keys are rejected."""
    policy = dict(DEFAULTS)
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        key, sep, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not sep or key not in DEFAULTS:
            raise ValueError(f"line {number}: unknown setting {key!r}")
        policy[key] = value
    checks = {
        "probe": ("admission", "liveness"),
        "spillover": ("regional", "global"),
        "anomaly_redirect": ("enabled", "disabled"),
        "placement.mail": ("primary", "alternate"),
        "placement.chat": ("primary", "alternate"),
    }
    for key, allowed in checks.items():
        if policy[key] not in allowed:
            raise ValueError(f"{key} must be one of {', '.join(allowed)}")
    for key in ("probe_interval", "probe_timeout", "spillover_below", "anomaly_threshold"):
        if float(policy[key]) <= 0:
            raise ValueError(f"{key} must be positive")
    for key in ("unhealthy_threshold", "healthy_threshold"):
        if int(policy[key]) < 1:
            raise ValueError(f"{key} must be at least 1")
    if not 0 <= int(policy["max_ejection_percent"]) <= 100:
        raise ValueError("max_ejection_percent must be between 0 and 100")
    return policy


def decide(units, health, policy):
    """Choose the serving units for each region.

    `units` maps unit -> region; `health` maps unit -> dict(state, last_ok,
    failed_now). Returns (targets: region -> sorted units, spilling: region ->
    reason or None, rotation: region -> units in rotation).
    """
    drained = set(policy["drained"].split())
    regions = sorted(set(units.values()))
    cap = int(policy["max_ejection_percent"])
    rotation, healthy_fraction, failing_fraction = {}, {}, {}
    for region in regions:
        members = sorted(u for u, r in units.items() if r == region and u not in drained)
        healthy = [u for u in members if health[u]["state"] == "healthy"]
        ejected = [u for u in members if health[u]["state"] != "healthy"]
        allowed = len(members) * cap // 100
        # Over the ejection cap, return the most recently healthy units.
        ejected.sort(key=lambda u: health[u]["last_ok"], reverse=True)
        kept = ejected[: max(0, len(ejected) - allowed)]
        rotation[region] = sorted(healthy + kept)
        healthy_fraction[region] = len(healthy) / len(members) if members else 0.0
        failing_fraction[region] = sum(1 for u in members if health[u]["failed_now"]) / len(members) if members else 1.0
    targets, spilling = {}, {}
    for region in regions:
        reason = None
        if policy["anomaly_redirect"] == "enabled" and failing_fraction[region] >= float(policy["anomaly_threshold"]):
            reason = f"anomaly: {failing_fraction[region]:.0%} of probes failing"
        elif policy["spillover"] == "global" and healthy_fraction[region] < float(policy["spillover_below"]):
            reason = f"spillover: {healthy_fraction[region]:.0%} of units healthy"
        serving = set(rotation[region])
        if reason:
            for other in regions:
                if other != region:
                    serving |= {u for u in rotation[other] if health[u]["state"] == "healthy"}
        if not serving:
            serving = {u for u, r in units.items() if r == region and u not in drained}
        targets[region] = sorted(serving)
        spilling[region] = reason
    return targets, spilling, rotation


class Kube:
    def __init__(self):
        self.namespace = Path(f"{SA}/namespace").read_text().strip()
        self.context = ssl.create_default_context(cafile=f"{SA}/ca.crt")

    def call(self, method, path, body=None, content_type="application/merge-patch+json"):
        token = Path(f"{SA}/token").read_text().strip()
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(API + path, data=data, method=method)
        request.add_header("Authorization", "Bearer " + token)
        if data is not None:
            request.add_header("Content-Type", content_type)
        with urllib.request.urlopen(request, context=self.context, timeout=10) as response:
            return json.loads(response.read() or b"{}")

    def ns(self, path):
        return f"/api/v1/namespaces/{self.namespace}{path}"


def probe(kind, ip, timeout):
    try:
        if kind == "admission":
            with socket.create_connection((ip, 8081), timeout=timeout) as s:
                s.settimeout(timeout)
                return s.recv(16).startswith(b"ok")
        with urllib.request.urlopen(f"http://{ip}:9100/livez", timeout=timeout) as response:
            return response.status == 200
    except OSError:
        return False


class Manager:
    def __init__(self):
        self.kube = Kube()
        self.health = {}
        self.status = {"units": {}, "regions": {}, "policy": {}, "error": None}
        self.counters = {}
        self.lock = threading.Lock()
        self.pool = ThreadPoolExecutor(32)
        self.last_reasons = {}

    def log(self, message):
        print(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), message, flush=True)

    def policy(self):
        cm = self.kube.call("GET", self.kube.ns("/configmaps/traffic-policy"))
        return parse_policy(cm.get("data", {}).get("policy", ""))

    def cycle(self):
        policy = self.policy()
        pods = self.kube.call("GET", self.kube.ns("/pods?labelSelector=app%3Dedge-gw"))["items"]
        live = {
            p["metadata"]["name"]: p
            for p in pods
            if p.get("status", {}).get("podIP") and not p["metadata"].get("deletionTimestamp")
        }
        units = {name: p["metadata"]["labels"].get("region", "") for name, p in live.items()}
        timeout = float(policy["probe_timeout"])
        results = dict(
            zip(
                live,
                self.pool.map(lambda n: probe(policy["probe"], live[n]["status"]["podIP"], timeout), live),
                strict=True,
            )
        )
        now = time.time()
        for name in list(self.health):
            if name not in live:
                del self.health[name]
        for name, ok in results.items():
            h = self.health.setdefault(
                name, {"state": "healthy", "ok_streak": 0, "fail_streak": 0, "last_ok": now, "failed_now": False}
            )
            h["failed_now"] = not ok
            self.counters.setdefault(name, {"probes": 0, "failures": 0})
            self.counters[name]["probes"] += 1
            if ok:
                h["ok_streak"], h["fail_streak"], h["last_ok"] = h["ok_streak"] + 1, 0, now
                if h["state"] != "healthy" and h["ok_streak"] >= int(policy["healthy_threshold"]):
                    h["state"] = "healthy"
                    self.log(f"readmitting {name}: {h['ok_streak']} consecutive {policy['probe']} probes passed")
            else:
                self.counters[name]["failures"] += 1
                h["ok_streak"], h["fail_streak"] = 0, h["fail_streak"] + 1
                if h["state"] == "healthy" and h["fail_streak"] >= int(policy["unhealthy_threshold"]):
                    h["state"] = "ejected"
                    self.log(f"ejecting {name}: {h['fail_streak']} consecutive {policy['probe']} probes failed")
        targets, spilling, rotation = decide(units, self.health, policy)
        for region, reason in spilling.items():
            if reason != self.last_reasons.get(region):
                if reason:
                    others = sorted(set(targets[region]) - set(rotation[region]))
                    self.log(f"region {region}: redirecting to {', '.join(others) or 'no other units'} ({reason})")
                else:
                    self.log(f"region {region}: serving from its own units again")
                self.last_reasons[region] = reason
        self.apply(live, targets, policy)
        with self.lock:
            self.status = {
                "time": now,
                "policy": policy,
                "error": None,
                "units": {
                    n: {
                        "region": units[n],
                        "state": "drained" if n in policy["drained"].split() else self.health[n]["state"],
                        "probe_ok": results[n],
                        "serving": sorted(r for r, t in targets.items() if n in t),
                        **self.counters[n],
                    }
                    for n in sorted(live)
                },
                "regions": {
                    r: {"serving": targets[r], "in_rotation": rotation[r], "redirect": spilling[r]} for r in targets
                },
            }
        return float(policy["probe_interval"])

    def apply(self, live, targets, policy):
        regions = sorted(targets)
        for name, pod in live.items():
            labels = pod["metadata"].get("labels", {})
            patch = {}
            for region in regions:
                want = "in" if name in targets[region] else None
                if labels.get(f"steer-{region}") != want:
                    patch[f"steer-{region}"] = want
            if patch:
                try:
                    self.kube.call("PATCH", self.kube.ns(f"/pods/{name}"), {"metadata": {"labels": patch}})
                except urllib.error.HTTPError as error:
                    if error.code != 404:
                        raise
        for service in SERVICES:
            for region in regions:
                if policy[f"placement.{service}"] == "alternate":
                    selector = {"app": "edge-alt"}
                else:
                    selector = {"app": "edge-gw", f"steer-{region}": "in"}
                path = self.kube.ns(f"/services/{service}-{region}")
                current = self.kube.call("GET", path)["spec"].get("selector", {})
                if current != selector:
                    self.kube.call(
                        "PATCH",
                        path,
                        [{"op": "replace", "path": "/spec/selector", "value": selector}],
                        "application/json-patch+json",
                    )
                    self.log(f"service {service}-{region} now selects {selector}")

    def run(self):
        while True:
            interval = 5.0
            try:
                interval = self.cycle()
            except Exception as error:  # keep steering on the last good state
                self.log(f"cycle failed: {error!r}")
                with self.lock:
                    self.status["error"] = repr(error)
            time.sleep(interval)

    def metrics(self):
        with self.lock:
            status = json.loads(json.dumps(self.status))
        lines = []
        for name, unit in status.get("units", {}).items():
            labels = f'unit="{name}",region="{unit["region"]}"'
            lines.append(f"traffic_manager_unit_healthy{{{labels}}} {int(unit['state'] == 'healthy')}")
            lines.append(f"traffic_manager_unit_drained{{{labels}}} {int(unit['state'] == 'drained')}")
            lines.append(f"traffic_manager_unit_regions_served{{{labels}}} {len(unit['serving'])}")
            lines.append(f"traffic_manager_probes_total{{{labels}}} {unit['probes']}")
            lines.append(f"traffic_manager_probe_failures_total{{{labels}}} {unit['failures']}")
        for region, info in status.get("regions", {}).items():
            lines.append(f'traffic_manager_region_serving_units{{region="{region}"}} {len(info["serving"])}')
            lines.append(f'traffic_manager_region_redirected{{region="{region}"}} {int(bool(info["redirect"]))}')
        return "\n".join(lines) + "\n"


def main():
    manager = Manager()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.startswith("/metrics"):
                body, kind = manager.metrics().encode(), "text/plain; version=0.0.4"
            elif self.path.startswith("/status"):
                with manager.lock:
                    body, kind = json.dumps(manager.status, indent=1).encode(), "application/json"
            elif self.path.startswith("/health"):
                body, kind = b"ok\n", "text/plain"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", kind)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    threading.Thread(target=ThreadingHTTPServer(("", 8080), Handler).serve_forever, daemon=True).start()
    manager.log(f"traffic manager started in namespace {manager.kube.namespace}")
    manager.run()


if __name__ == "__main__":
    main()
