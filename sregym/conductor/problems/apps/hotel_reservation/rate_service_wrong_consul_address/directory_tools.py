"""Cluster-only inspection and restoration of native service registrations."""

import json
import os
import re
import subprocess
import sys
import time

namespace = os.environ.get("NAMESPACE", "hotel-reservation")


def k(*args, stdin=None):
    return subprocess.check_output(["kubectl", "-n", namespace, *args], input=stdin, text=True)


def remote(code):
    return k("exec", "-i", "deploy/user", "--", "python3", "-", stdin=code)


if sys.argv[1] == "snapshot":
    print(
        remote(
            "import urllib.request\nprint(urllib.request.urlopen('http://consul:8500/v1/agent/services',timeout=10).read().decode())"
        ),
        end="",
    )
    sys.exit(0)

scopes = {"rate": 8084}
if sys.argv[1] == "recover":
    scopes["user"] = 8086
live = {}
for component, port in scopes.items():
    for _attempt in range(90):
        observed = {}
        pods = json.loads(k("get", "pods", "-l", "io.kompose.service=" + component, "-o", "json"))["items"]
        candidates = [
            pod for pod in pods if not pod["metadata"].get("deletionTimestamp") and pod.get("status", {}).get("podIP")
        ]
        for pod in candidates:
            for container in pod["spec"]["containers"]:
                if not any(p.get("containerPort") == port for p in container.get("ports", [])):
                    continue
                try:
                    logs = k("logs", pod["metadata"]["name"], "-c", container["name"])
                except subprocess.CalledProcessError:
                    continue
                lines = [
                    line
                    for line in logs.splitlines()
                    if "Trying to register service" in line and "srv-" + component in line
                ]
                if not lines:
                    continue
                ids = re.findall(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", lines[-1])
                if ids:
                    observed[ids[-1]] = {"address": pod["status"]["podIP"], "name": "srv-" + component, "port": port}
        if observed and len(observed) == len(candidates):
            live.update(observed)
            break
        time.sleep(1)
    else:
        raise RuntimeError("No complete live " + component + " registration set found in application startup logs")

original = {}
if sys.argv[1] == "recover":
    path = os.path.join(
        os.environ.get("ARTIFACT_DIR", os.path.dirname(os.path.abspath(__file__))), "directory-original.json"
    )
    if os.path.exists(path):
        with open(path) as stream:
            original = json.load(stream)

code = """import json, urllib.request
live = LIVE
original = ORIGINAL
names = set(value['name'] for value in live.values())
base = 'http://consul:8500'
def call(path, value=None):
    req = urllib.request.Request(base + path, data=None if value is None else json.dumps(value).encode(), method='GET' if value is None else 'PUT')
    if value is not None: req.add_header('Content-Type', 'application/json')
    with urllib.request.urlopen(req, timeout=15) as response:
        raw = response.read()
        return json.loads(raw) if raw else None
entries = call('/v1/agent/services')
for sid, item in entries.items():
    if item['Service'] not in names: continue
    if sid not in live:
        call('/v1/agent/service/deregister/' + sid, {})
        continue
    endpoint = live[sid]
    saved = original.get(sid)
    if saved and saved['Address'] == endpoint['address']: item = saved
    result = {'ID': sid, 'Name': endpoint['name'], 'Address': endpoint['address'], 'Port': endpoint['port']}
    for key in ('Tags', 'Meta', 'Weights', 'EnableTagOverride'):
        if key in item: result[key] = item[key]
    result['TaggedAddresses'] = {key: dict(value, Address=endpoint['address'], Port=endpoint['port']) for key, value in item.get('TaggedAddresses', {}).items()}
    call('/v1/agent/service/register', result)
for sid, endpoint in live.items():
    if sid not in entries:
        call('/v1/agent/service/register', {'ID': sid, 'Name': endpoint['name'], 'Address': endpoint['address'], 'Port': endpoint['port']})
print('Restored the live service directory')
""".replace("LIVE", repr(live)).replace("ORIGINAL", repr(original))
print(remote(code), end="")
