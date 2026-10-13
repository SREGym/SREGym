#!/usr/bin/env python3
"""Cluster operations for the recurring directory recovery incident."""
import base64
import json
import os
import pathlib
import re
import subprocess
import sys
import time
import urllib.parse

NS = os.environ.get("NAMESPACE", "hotel-reservation")
ROOT = pathlib.Path(os.environ.get("ARTIFACT_DIR", pathlib.Path(__file__).resolve().parent))
STATE = ROOT / "state"


def k(*args, data=None, check=True):
    p = subprocess.run(["kubectl", "-n", NS, *args], input=data,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if check and p.returncode:
        raise RuntimeError(p.stderr.decode(errors="replace"))
    return p.stdout


def obj(*args):
    return json.loads(k("get", *args, "-o", "json"))


def pods(component=None):
    args = ["pods"]
    if component:
        args += ["-l", "io.kompose.service=" + component]
    return [p for p in obj(*args)["items"]
            if p.get("status", {}).get("phase") == "Running"
            and not p["metadata"].get("deletionTimestamp")]


def api_pod():
    choices = pods("recommendation")
    if not choices:
        raise RuntimeError("No running recommendation container")
    return choices[0]["metadata"]["name"]


HTTP = r'''
import base64,json,sys,urllib.request
method,path=sys.argv[1:3]
body=sys.stdin.buffer.read()
r=urllib.request.Request('http://consul:8500'+path,data=body if method!='GET' else None,method=method)
with urllib.request.urlopen(r,timeout=130) as x:
    print(json.dumps({'headers':dict(x.headers),'data':base64.b64encode(x.read()).decode()}))
'''


def api(method, path, data=b""):
    out = json.loads(k("exec", "-i", api_pod(), "--", "python3", "-c", HTTP,
                       method, path, data=data))
    return base64.b64decode(out["data"]), out["headers"]


def get(path):
    return json.loads(api("GET", path)[0])


def put(path, value=None, raw=None):
    data = raw if raw is not None else json.dumps(value).encode()
    return api("PUT", path, data)[0]


def registration(s):
    return {"ID": s["ID"], "Name": s.get("Service", s.get("Name")),
            "Address": s["Address"], "Port": s["Port"],
            "Tags": s.get("Tags") or [], "Meta": s.get("Meta") or {},
            "Weights": s.get("Weights") or {"Passing": 1, "Warning": 1},
            "EnableTagOverride": s.get("EnableTagOverride", False)}


def logging_config(signature, restore=False):
    path = STATE / "logging.json"
    deployment = obj("deployment", "consul")
    containers = deployment["spec"]["template"]["spec"]["containers"]
    current = containers[0]
    saved = json.loads(path.read_text()) if path.exists() else None
    if restore:
        if not saved or saved["signature"] != signature:
            return False
        desired_env = saved["env"]
    else:
        STATE.mkdir(exist_ok=True)
        if not saved or saved["signature"] != signature:
            saved = {"signature": signature, "env": current.get("env")}
            path.write_text(json.dumps(saved))
        desired_env = list(saved["env"] or [])
        config = next((e for e in desired_env if e["name"] == "CONSUL_LOCAL_CONFIG"), None)
        if config and "valueFrom" in config:
            raise RuntimeError("This deployment uses an external Consul configuration")
        value = json.loads(config["value"]) if config else {}
        value["log_level"] = "INFO"
        desired_env = [e for e in desired_env if e["name"] != "CONSUL_LOCAL_CONFIG"]
        desired_env.append({"name": "CONSUL_LOCAL_CONFIG", "value": json.dumps(value)})
    if current.get("env") == desired_env:
        return False
    if desired_env is None:
        current.pop("env", None)
    else:
        current["env"] = desired_env
    # JSON Patch replaces the container list, including removal of the env field.
    patch = [{"op": "replace", "path": "/spec/template/spec/containers", "value": containers}]
    k("patch", "deployment", "consul", "--type=json", "-p", json.dumps(patch))
    print(k("rollout", "status", "deployment/consul", "--timeout=180s").decode().strip(), flush=True)
    return True


STOP = r'''
import os,signal
stopped=[]
for name in os.listdir('/proc'):
    if not name.isdigit() or int(name)==os.getpid(): continue
    try:
        raw=open('/proc/'+name+'/cmdline','rb').read()
        parts=raw.split(b'\0')
        if len(parts)>2 and b'python' in parts[0] and parts[1]==b'-c':
            code=parts[2]
            if b'/v1/snapshot' in code and b'/v1/agent/service/deregister/' in code and b'time.sleep' in code:
                os.kill(int(name),signal.SIGTERM)
                stopped.append(name)
    except (OSError,ProcessLookupError): pass
print('Stopped directory recovery sessions: '+','.join(stopped))
'''

FIND = r'''
import json,os
found=[]
for name in os.listdir('/proc'):
    if not name.isdigit() or int(name)==os.getpid(): continue
    try:
        parts=open('/proc/'+name+'/cmdline','rb').read().split(b'\0')
        if len(parts)>2 and b'python' in parts[0] and parts[1]==b'-c' and b'/v1/snapshot' in parts[2] and b'/v1/agent/service/deregister/' in parts[2] and b'time.sleep' in parts[2]:
            found.append(int(name))
    except OSError: pass
print(json.dumps(found))
'''


def stop_workers():
    # Discover the operation in live process arguments, without a saved PID or path.
    for p in pods():
        component = p["metadata"].get("labels", {}).get("io.kompose.service", "")
        if component in {"frontend", "geo", "profile", "rate", "recommendation", "reservation", "search", "user"}:
            for c in p["spec"]["containers"]:
                result = k("exec", p["metadata"]["name"], "-c", c["name"], "--",
                           "python3", "-c", STOP, check=False)
                if result:
                    print(p["metadata"]["name"] + ": " + result.decode().strip(), flush=True)


def reconcile():
    services = get("/v1/agent/services")
    components = {"srv-" + c: c for c in
                  ("geo", "profile", "rate", "recommendation", "reservation", "search", "user")}
    desired = {}
    for name, component in components.items():
        service = obj("service", component)
        target = service["spec"]["ports"][0].get("targetPort", service["spec"]["ports"][0]["port"])
        for p in pods(component):
            ip = p["status"].get("podIP")
            if not ip:
                continue
            port = target
            if not isinstance(port, int):
                port = next(x["containerPort"] for c in p["spec"]["containers"]
                            for x in c.get("ports", []) if x.get("name") == target)
            logs = k("logs", p["metadata"]["name"], "--tail=500", check=False).decode(errors="replace")
            identities = re.findall(r"name: " + re.escape(name) + r", id: ([0-9a-f-]+), address: " + re.escape(ip) + r":" + str(port), logs)
            existing = [x for x in services.values() if x["Service"] == name
                        and x["Address"] == ip and x["Port"] == port]
            ident = identities[-1] if identities else (existing[0]["ID"] if existing else p["metadata"]["uid"])
            desired[ident] = {"ID": ident, "Name": name, "Address": ip, "Port": port,
                              "Tags": [], "Meta": {}, "Weights": {"Passing": 1, "Warning": 1}}
    for ident, value in services.items():
        if value["Service"] in components and ident not in desired:
            put("/v1/agent/service/deregister/" + urllib.parse.quote(ident), {})
    for value in desired.values():
        put("/v1/agent/service/register", value)
    # Native snapshot restoration can leave catalog-only records absent from
    # the agent's local inventory. Remove obsolete managed records there too.
    live_nodes = {n["Name"] for n in get("/v1/agent/members")}
    for node in get("/v1/catalog/nodes"):
        catalog = get("/v1/catalog/node/" + urllib.parse.quote(node["Node"]))
        if not catalog:
            continue
        names = {x["Service"] for x in catalog.get("Services", {}).values()}
        if node["Node"] not in live_nodes and names <= set(components) | {"consul"}:
            put("/v1/catalog/deregister", {"Node": node["Node"]})
            continue
        for value in catalog.get("Services", {}).values():
            if value["Service"] not in components:
                continue
            wanted = desired.get(value["ID"])
            if not wanted or value["Address"] != wanted["Address"] or value["Port"] != wanted["Port"]:
                put("/v1/catalog/deregister", {"Node": node["Node"], "ServiceID": value["ID"]})
    actual = get("/v1/agent/services")
    for ident, value in desired.items():
        assert ident in actual and actual[ident]["Address"] == value["Address"]
    print("Live RPC registrations reconciled from pods, Service ports, and startup logs.", flush=True)


WORKER = r'''
import base64,ctypes,json,os,signal,sys,time,urllib.request
payload=json.load(sys.stdin)
snapshot=base64.b64decode(payload['snapshot'])
members=payload['members']
def request(method,path,body=None):
    req=urllib.request.Request('http://consul:8500'+path,data=body,method=method)
    with urllib.request.urlopen(req,timeout=60) as response:
        return response.read()
def restore():
    current=json.loads(request('GET','/v1/agent/services'))
    for ident,member in current.items():
        if member['Service']=='srv-rate':
            request('PUT','/v1/agent/service/deregister/'+ident,b'{}')
    for member in members:
        request('PUT','/v1/agent/service/register',json.dumps(member).encode())
    request('PUT','/v1/snapshot',snapshot)
restore()
pid=os.fork()
if pid:
    print('Directory recovery session active.',flush=True)
    os._exit(0)
os.setsid()
null=os.open('/dev/null',os.O_RDWR)
for fd in (0,1,2): os.dup2(null,fd)
if null>2: os.close(null)
ctypes.CDLL(None).prctl(15,b'catalog-recover',0,0,0)
signal.signal(signal.SIGTERM,lambda *_:sys.exit(0))
while True:
    time.sleep(210)
    try: restore()
    except Exception: pass
'''


def wait_replacement(old_names, component="rate"):
    args = ["kubectl", "-n", NS, "get", "pods", "-l", "io.kompose.service=" + component, "--watch",
            "--no-headers", "-o", "custom-columns=NAME:.metadata.name,DELETING:.metadata.deletionTimestamp,READY:.status.containerStatuses[*].ready,IP:.status.podIP"]
    p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        for line in p.stdout:
            fields = line.split()
            if len(fields) == 4 and fields[0] not in old_names and fields[1] == "<none>" and fields[2] == "true" and fields[3] != "<none>":
                return fields[0], fields[3]
        raise RuntimeError("Pod watch ended before a ready replacement appeared")
    finally:
        p.terminate()
        p.wait(timeout=10)


def wait_registration(component, ip):
    path = "/v1/health/service/srv-" + component
    data, headers = api("GET", path)
    limit = time.monotonic() + 180
    while time.monotonic() < limit:
        if any(x["Service"]["Address"] == ip for x in json.loads(data)):
            return
        index = next(v for key, v in headers.items() if key.lower() == "x-consul-index")
        data, headers = api("GET", path + "?index=" + index + "&wait=60s")
    raise RuntimeError("The replacement did not register: " + component)


def prepare(signature):
    STATE.mkdir(exist_ok=True)
    reconcile()
    members = [registration(x) for x in get("/v1/agent/services").values() if x["Service"] == "srv-rate"]
    assert members, "No live rate registration available for the recovery snapshot"
    snapshot = api("GET", "/v1/snapshot")[0]
    health, headers = api("GET", "/v1/health/service/srv-rate")
    index = next(v for key, v in headers.items() if key.lower() == "x-consul-index")
    old_names = {p["metadata"]["name"] for p in pods("rate")}
    for name in sorted(old_names):
        print(k("delete", "pod", name, "--wait=true").decode().strip(), flush=True)
    new_name, new_ip = wait_replacement(old_names)
    limit = time.monotonic() + 180
    while time.monotonic() < limit:
        data, hdr = api("GET", "/v1/health/service/srv-rate?index=" + index + "&wait=60s")
        values = json.loads(data)
        if any(x["Service"]["Address"] == new_ip for x in values):
            break
        index = next(v for key, v in hdr.items() if key.lower() == "x-consul-index")
    else:
        raise RuntimeError("The replacement did not register in Consul")
    reconcile()
    healthy = api("GET", "/v1/snapshot")[0]
    record = {"signature": signature, "snapshot": base64.b64encode(snapshot).decode(), "members": members,
              "healthy_snapshot": base64.b64encode(healthy).decode()}
    (STATE / "directory.json").write_text(json.dumps(record))
    print("Captured catalog generations around rate instance replacement.", flush=True)
    return record


def inject():
    signature = obj("deployment", "consul")["metadata"]["uid"]
    path = STATE / "directory.json"
    record = json.loads(path.read_text()) if path.exists() else None
    if record and record["signature"] == signature:
        active = json.loads(k("exec", api_pod(), "--", "python3", "-c", FIND))
        routes = [s["Address"] for s in get("/v1/agent/services").values() if s["Service"] == "srv-rate"]
        live = {p["status"]["podIP"] for p in pods("rate")}
        if active and routes and not set(routes) & live:
            print("Directory recovery session already active; no duplicate started.", flush=True)
            return
    stop_workers()
    if logging_config(signature):
        record = None
    if not record or record["signature"] != signature:
        record = prepare(signature)
    # Stored identities must be retired, even after re-running this command.
    live = {p["status"]["podIP"] for p in pods("rate")}
    if any(s["Address"] in live for s in record["members"]):
        record = prepare(signature)
    print(k("exec", "-i", api_pod(), "--", "python3", "-c", WORKER,
            data=json.dumps(record).encode()).decode().strip(), flush=True)
    current = get("/v1/agent/services")
    routes = [s["Address"] for s in current.values() if s["Service"] == "srv-rate"]
    assert routes and not set(routes) & {p["status"]["podIP"] for p in pods("rate")}
    print("Directory recovery publication completed.", flush=True)


def mitigate():
    stop_workers()
    reconcile()


def recover():
    stop_workers()
    path = STATE / "directory.json"
    if path.exists():
        saved = json.loads(path.read_text())
        if saved["signature"] == obj("deployment", "consul")["metadata"]["uid"]:
            put("/v1/snapshot", raw=base64.b64decode(saved["healthy_snapshot"]))
    reconcile()
    if logging_config(obj("deployment", "consul")["metadata"]["uid"], restore=True):
        reconcile()
    # PID 1 in this application image does not reap orphaned children. Recycle
    # only containers containing defunct recovery sessions, then reconcile.
    recycled = False
    for p in pods("recommendation"):
        code = """import pathlib
found=False
for x in pathlib.Path('/proc').glob('[0-9]*/comm'):
    try: found=found or x.read_text().strip()=='catalog-recover'
    except OSError: pass
print(found)
"""
        if k("exec", p["metadata"]["name"], "--", "python3", "-c", code).strip() == b"True":
            k("delete", "pod", p["metadata"]["name"], "--wait=true")
            _, ip = wait_replacement({p["metadata"]["name"]}, "recommendation")
            wait_registration("recommendation", ip)
            recycled = True
    if recycled:
        reconcile()


if __name__ == "__main__":
    {"inject": inject, "mitigate": mitigate, "recover": recover}[sys.argv[1]]()
