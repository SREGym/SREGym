import json
import os
import ssl
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pymysql

CLUSTER = os.environ["CLUSTER"]
INSTANCES = json.loads(os.environ["INSTANCES"])
ROUTE = os.environ["ROUTE"]
STATE = os.environ["STATE"]
STATE_NAMESPACE = os.environ["STATE_NAMESPACE"]
POLL = float(os.environ["POLL_SECONDS"])
DEAD_AFTER = int(os.environ["DEAD_AFTER_POLLS"])
BLOCK = float(os.environ["RECOVERY_PERIOD_BLOCK_SECONDS"])
ROOT_PASSWORD = os.environ["MYSQL_ROOT_PASSWORD"]
REPLICATION_PASSWORD = os.environ["REPLICATION_PASSWORD"]
PORT = 3306
ACCOUNT = "/var/run/secrets/kubernetes.io/serviceaccount"
NAMESPACE = Path(f"{ACCOUNT}/namespace").read_text().strip()
TLS = ssl.create_default_context()
TLS.check_hostname = False
TLS.verify_mode = ssl.CERT_NONE
lock = threading.RLock()
failures = {name: 0 for name in INSTANCES}
topology = {}


class APIError(Exception):
    pass


def log(level, message):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), level, message, flush=True)


def kube(method, path, body=None):
    context = ssl.create_default_context(cafile=f"{ACCOUNT}/ca.crt")
    request = urllib.request.Request(
        "https://kubernetes.default.svc" + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            "Authorization": "Bearer " + Path(f"{ACCOUNT}/token").read_text().strip(),
            "Content-Type": "application/merge-patch+json",
            "User-Agent": "orchestrator",
        },
        method=method,
    )
    with urllib.request.urlopen(request, context=context, timeout=10) as response:
        return json.loads(response.read())


def load_state():
    state = json.loads(kube("GET", f"/api/v1/namespaces/{STATE_NAMESPACE}/configmaps/{STATE}")["data"]["state.json"])
    state.setdefault("downtime", {})
    state.setdefault("candidates", {})
    state.setdefault("acknowledged", 0)
    return state


def save_state(state):
    kube(
        "PATCH", f"/api/v1/namespaces/{STATE_NAMESPACE}/configmaps/{STATE}", {"data": {"state.json": json.dumps(state)}}
    )


def connect(name, read_timeout=10):
    return pymysql.connect(
        host=INSTANCES[name]["host"],
        user="root",
        password=ROOT_PASSWORD,
        connect_timeout=3,
        read_timeout=read_timeout,
        write_timeout=10,
        ssl=TLS,
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=True,
    )


def query(name, *statements, read_timeout=10):
    connection = connect(name, read_timeout)
    try:
        with connection.cursor() as cursor:
            rows = None
            for statement in statements:
                cursor.execute(statement)
                rows = cursor.fetchall()
            return rows
    finally:
        connection.close()


def instance(host):
    """An instance by its key or hostname, as the API's host arguments name it."""
    for name, config in INSTANCES.items():
        if host in (name, config["host"], f"{config['host']}.{NAMESPACE}.svc"):
            return name
    raise APIError(f"Cannot read instance: {host}:{PORT}")


def probe(name):
    try:
        variables = query(
            name,
            "SELECT @@read_only AS ro, @@super_read_only AS sro, @@gtid_executed AS gtid, "
            "@@server_id AS sid, @@server_uuid AS uuid, @@version AS version;",
        )[0]
        replica = query(name, "SHOW REPLICA STATUS;")
    except Exception as exc:
        return {
            "Key": {"Hostname": INSTANCES[name]["host"], "Port": PORT},
            "Name": name,
            "IsLastCheckValid": False,
            "LastError": str(exc),
        }
    status = replica[0] if replica else {}
    return {
        "Key": {"Hostname": INSTANCES[name]["host"], "Port": PORT},
        "Name": name,
        "ServerID": variables["sid"],
        "ServerUUID": variables["uuid"],
        "Version": variables["version"],
        "DataCenter": INSTANCES[name]["region"],
        "IsLastCheckValid": True,
        "ReadOnly": bool(variables["ro"]),
        "SuperReadOnly": bool(variables["sro"]),
        "ExecutedGtidSet": variables["gtid"].replace("\n", ""),
        "MasterKey": {"Hostname": status.get("Source_Host", ""), "Port": status.get("Source_Port", 0)},
        "ReplicationIOThreadRuning": status.get("Replica_IO_Running") == "Yes",
        "ReplicationSQLThreadRuning": status.get("Replica_SQL_Running") == "Yes",
        "SecondsBehindMaster": status.get("Seconds_Behind_Source"),
        "LastIOError": status.get("Last_IO_Error", ""),
        "LastSQLError": status.get("Last_SQL_Error", ""),
    }


def refresh():
    for name in INSTANCES:
        topology[name] = probe(name)


def route(name):
    region = INSTANCES[name]["region"]
    service = kube("GET", f"/api/v1/namespaces/{NAMESPACE}/services/{ROUTE}")
    if service["spec"].get("selector", {}).get("region") != region:
        kube("PATCH", f"/api/v1/namespaces/{NAMESPACE}/services/{ROUTE}", {"spec": {"selector": {"region": region}}})
        log("INFO", f"Topology: master of cluster {CLUSTER} is {name}; pointed {ROUTE} at region {region}")


def replicas_of(master):
    host = INSTANCES[master]["host"]
    return [
        n
        for n, t in topology.items()
        if n != master and t.get("IsLastCheckValid") and t["MasterKey"]["Hostname"] == host
    ]


def errant(designated, master):
    gtids = (topology[designated]["ExecutedGtidSet"], topology[master]["ExecutedGtidSet"])
    return query(master, f"SELECT GTID_SUBTRACT('{gtids[0]}', '{gtids[1]}') AS errant;")[0]["errant"]


def downtimed(state, name):
    window = state["downtime"].get(name)
    return bool(window) and window["EndsAt"] > time.time()


def audit(state, analysis, failed, successor):
    state["audit"].append(
        {
            "RecoveryId": len(state["audit"]) + 1,
            "ClusterAlias": CLUSTER,
            "Analysis": analysis,
            "AnalyzedInstanceKey": failed,
            "SuccessorKey": successor,
            "IsSuccessful": True,
            "Acknowledged": False,
            "RecoveryEndTimestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
    )
    state["last_recovery"] = time.time()


def promote(state, analysis, failed, successor):
    """Detach the successor and open it for writes, as ApplyMySQLPromotionAfterMasterFailover does."""
    query(successor, "STOP REPLICA;", "RESET REPLICA ALL;", "SET GLOBAL read_only = OFF;")
    state["master"] = successor
    audit(state, analysis, failed, successor)
    save_state(state)
    route(successor)


def candidates(state, master):
    found = [n for n in replicas_of(master) if topology[n]["ReplicationSQLThreadRuning"]]
    order = {"prefer": 0, "neutral": 1, "prefer_not": 2, "must_not": 3}
    found = [n for n in found if state["candidates"].get(n) != "must_not"]
    return sorted(found, key=lambda n: order.get(state["candidates"].get(n, "neutral"), 1))


def blocked(state):
    return time.time() - state.get("last_recovery", 0) < BLOCK and state["acknowledged"] < state.get("last_recovery", 0)


def recover_dead_master(state, master):
    if downtimed(state, master):
        log("INFO", f"DeadMaster {master}: instance is downtimed; skipping recovery")
        return None
    if blocked(state):
        log("WARNING", f"DeadMaster {master}: blocked by RecoveryPeriodBlockSeconds since the last recovery")
        return None
    found = candidates(state, master)
    if not found:
        log("ERROR", f"DeadMaster {master}: no replica to promote")
        return None
    log("INFO", f"topology_recovery: DeadMaster {master}; promoting {found[0]}")
    promote(state, "DeadMaster", master, found[0])
    return found[0]


def takeover(designated, start_replication):
    with lock:
        state = load_state()
        master = state["master"]
        if designated == master:
            raise APIError(f"GracefulMasterTakeover: {designated} is already the master of {CLUSTER}")
        refresh()
        if designated not in replicas_of(master):
            raise APIError(
                f"GracefulMasterTakeover: designated instance {designated} is not a direct replica of {master}"
            )
        if found := errant(designated, master):
            raise APIError(f"GracefulMasterTakeover: errant GTID {found} found on {designated}")
        log("INFO", f"GracefulMasterTakeover: setting {master} read_only; designated {designated}")
        query(master, "SET GLOBAL read_only = ON;")
        try:
            gtid = probe(master)["ExecutedGtidSet"]
            waited = query(designated, f"SELECT WAIT_FOR_EXECUTED_GTID_SET('{gtid}', 30) AS waited;", read_timeout=45)[
                0
            ]["waited"]
        except Exception as exc:
            waited = exc
        if waited != 0:
            query(master, "SET GLOBAL read_only = OFF;")
            raise APIError(f"GracefulMasterTakeover: {designated} did not catch up with {master}: {waited}")
        promote(state, "GracefulMasterTakeover", master, designated)
        point(master, designated, start_replication)
        message = f"{designated} is master of {CLUSTER}; {master} is set to replicate from it"
        log("INFO", "GracefulMasterTakeover: " + message)
        return message


def force_takeover(designated, analysis):
    """Promote at once, leaving the old master as it is, as an operator forcing a failover asked."""
    with lock:
        state = load_state()
        master = state["master"]
        refresh()
        if designated is None:
            found = candidates(state, master)
            if not found:
                raise APIError(f"{analysis}: no replica of {master} to promote")
            designated = found[0]
        if designated == master:
            raise APIError(f"{analysis}: {designated} is already the master of {CLUSTER}")
        log("INFO", f"{analysis}: promoting {designated}; {master} is not contacted")
        promote(state, analysis, master, designated)
        return f"{designated} is master of {CLUSTER}"


def point(name, source, start):
    host = INSTANCES[source]["host"]
    query(
        name,
        "STOP REPLICA;",
        f"CHANGE REPLICATION SOURCE TO SOURCE_HOST='{host}', SOURCE_PORT={PORT}, SOURCE_USER='replicator', "
        f"SOURCE_PASSWORD='{REPLICATION_PASSWORD}', SOURCE_AUTO_POSITION=1, GET_SOURCE_PUBLIC_KEY=1;",
        *(["START REPLICA;"] if start else []),
    )


def analysis():
    state = load_state()
    master = state["master"]
    entries = []
    if not topology.get(master, {}).get("IsLastCheckValid", True):
        kind = "DeadMaster" if failures[master] >= DEAD_AFTER else "UnreachableMaster"
        entries.append({"Analysis": kind, "AnalyzedInstanceKey": master})
    for name, t in topology.items():
        if name == master or not t.get("IsLastCheckValid"):
            continue
        if not t["MasterKey"]["Hostname"]:
            kind = "NotConnectedToMaster"
        elif not t["ReadOnly"]:
            kind = "ReplicaNotReadOnly"
        elif not (t["ReplicationIOThreadRuning"] and t["ReplicationSQLThreadRuning"]):
            kind = "ReplicationStopped"
        else:
            continue
        entries.append({"Analysis": kind, "AnalyzedInstanceKey": name})
    return [{"ClusterAlias": CLUSTER, **e, "IsDowntimed": downtimed(state, e["AnalyzedInstanceKey"])} for e in entries]


def tabulated(state):
    lines = []
    master = state["master"]
    ordered = [master, *sorted(n for n in INSTANCES if n != master)]
    for name in ordered:
        t = topology.get(name, {})
        if not t.get("IsLastCheckValid"):
            flags = "[unknown,invalid]"
        else:
            lag = t["SecondsBehindMaster"]
            replication = (
                "nonreplicating" if t["MasterKey"]["Hostname"] and not t["ReplicationSQLThreadRuning"] else "ok"
            )
            flags = (
                f"[{lag if lag is not None else 0}s,{replication},{t['Version']},"
                f"{'ro' if t['ReadOnly'] else 'rw'},ROW,>>,GTID{',downtimed' if downtimed(state, name) else ''}]"
            )
        indent = "" if name == master else "+ "
        lines.append(f"{indent}{INSTANCES[name]['host']}:{PORT}   {flags}")
    return "\n".join(lines)


def watch():
    while True:
        try:
            with lock:
                refresh()
                state = load_state()
                master = state["master"]
                failures[master] = 0 if topology[master]["IsLastCheckValid"] else failures[master] + 1
                if failures[master]:
                    log("WARNING", f"{master}: check failed ({failures[master]}): {topology[master]['LastError']}")
                if state["recoveries_enabled"]:
                    if failures[master] >= DEAD_AFTER:
                        recover_dead_master(state, master)
                    elif topology[master]["IsLastCheckValid"]:
                        route(master)
        except Exception as exc:
            log("ERROR", f"discovery: {exc}")
        time.sleep(POLL)


def update(change, message):
    with lock:
        state = load_state()
        change(state)
        save_state(state)
    log("INFO", message)
    return {"Code": "OK", "Message": message}


def instance_command(command, name, rest):
    if command == "instance":
        return probe(name)
    if command == "discover":
        topology[name] = probe(name)
        return {"Code": "OK", "Message": f"Instance discovered: {name}", "Details": topology[name]}
    statements = {
        "set-read-only": ["SET GLOBAL read_only = ON;"],
        "set-writeable": ["SET GLOBAL read_only = OFF;"],
        "start-replica": ["START REPLICA;"],
        "stop-replica": ["STOP REPLICA;"],
        "reset-replica": ["STOP REPLICA;", "RESET REPLICA ALL;"],
    }
    if command in statements:
        query(name, *statements[command])
        topology[name] = probe(name)
        log("INFO", f"{command}: {name}")
        return {"Code": "OK", "Message": f"{command}: {name}", "Details": topology[name]}
    if command == "relocate":
        if len(rest) < 1:
            raise APIError("relocate: missing destination instance")
        below = instance(rest[0])
        if below == name:
            raise APIError(f"relocate: cannot relocate {name} below itself")
        point(name, below, True)
        topology[name] = probe(name)
        log("INFO", f"relocate: {name} below {below}")
        return {"Code": "OK", "Message": f"Instance relocated: {name} below {below}", "Details": topology[name]}
    if command == "begin-downtime":
        owner, reason = (rest + ["", ""])[:2]
        seconds = int(rest[2]) * 60 if len(rest) > 2 and rest[2].isdigit() else 3600

        def change(state):
            state["downtime"][name] = {"Owner": owner, "Reason": reason, "EndsAt": time.time() + seconds}

        return update(change, f"Downtimed {name} for {seconds}s: {reason}")
    if command == "end-downtime":
        return update(lambda state: state["downtime"].pop(name, None), f"Ended downtime of {name}")
    if command == "register-candidate":
        rule = rest[0] if rest else "neutral"
        return update(lambda state: state["candidates"].__setitem__(name, rule), f"Registered {name} as {rule}")
    if command == "recover":
        with lock:
            refresh()
            state = load_state()
            if name != state["master"] or topology[name]["IsLastCheckValid"]:
                return {"Code": "OK", "Message": f"No actionable recovery found for {name}"}
            successor = recover_dead_master(state, name)
        return {"Code": "OK", "Message": f"Recovered: {successor}" if successor else f"Recovery skipped for {name}"}
    return None


def handle(parts):
    command, arguments = parts[0], parts[1:]
    if command == "health":
        return {"Code": "OK", "Message": "Application node is healthy"}
    if command == "clusters":
        return [CLUSTER]
    if command == "clusters-info":
        state = load_state()
        return [
            {
                "ClusterName": INSTANCES[state["master"]]["host"] + f":{PORT}",
                "ClusterAlias": CLUSTER,
                "CountInstances": len(INSTANCES),
                "HasAutomatedMasterRecovery": state["recoveries_enabled"],
            }
        ]
    if command in ("cluster", "topology-json"):
        with lock:
            master = load_state()["master"]
            return [{**t, "IsMaster": n == master} for n, t in topology.items()]
    if command in ("topology", "topology-tabulated"):
        with lock:
            return {"Code": "OK", "Details": tabulated(load_state())}
    if command == "replication-analysis":
        with lock:
            return {"Code": "OK", "Details": analysis()}
    if command == "audit-recovery":
        return list(reversed(load_state()["audit"]))
    if command == "blocked-recoveries":
        state = load_state()
        return (
            [{"ClusterAlias": CLUSTER, "LastRecoveryTimestamp": state.get("last_recovery")}] if blocked(state) else []
        )
    if command in ("ack-all-recoveries", "ack-cluster-recoveries", "ack-recovery"):
        return update(lambda state: state.__setitem__("acknowledged", time.time()), "Acknowledged recoveries")
    if command == "check-global-recoveries":
        return {"Code": "OK", "Details": load_state()["recoveries_enabled"]}
    if command in ("disable-global-recoveries", "enable-global-recoveries"):
        enabled = command.startswith("enable")
        return update(
            lambda state: state.__setitem__("recoveries_enabled", enabled),
            ("Enabled" if enabled else "Disabled") + " global recoveries",
        )
    if command in ("graceful-master-takeover", "graceful-master-takeover-auto"):
        if len(arguments) < 2:
            raise APIError(f"{command}: expected /api/{command}/:clusterHint/:designatedHost/:designatedPort")
        return {"Code": "OK", "Message": takeover(instance(arguments[1]), command.endswith("auto"))}
    if command == "force-master-failover":
        return {"Code": "OK", "Message": force_takeover(None, "ForceMasterFailover")}
    if command == "force-master-takeover":
        if len(arguments) < 2:
            raise APIError(f"{command}: expected /api/{command}/:clusterHint/:designatedHost/:designatedPort")
        return {"Code": "OK", "Message": force_takeover(instance(arguments[1]), "ForceMasterTakeover")}
    if arguments:
        name = instance(arguments[0])
        rest = arguments[1:]
        if rest and rest[0].isdigit():
            rest = rest[1:]
        if (result := instance_command(command, name, rest)) is not None:
            return result
    raise KeyError(command)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, code, data):
        payload = json.dumps(data, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        parts = [urllib.request.unquote(p) for p in self.path.split("?")[0].split("/") if p]
        if len(parts) < 2 or parts[0] != "api":
            return self.reply(404, {"Code": "ERROR", "Message": "Not found"})
        try:
            return self.reply(200, handle(parts[1:]))
        except KeyError:
            return self.reply(404, {"Code": "ERROR", "Message": f"Unknown command: {parts[1]}"})
        except APIError as exc:
            return self.reply(500, {"Code": "ERROR", "Message": str(exc)})
        except Exception as exc:
            log("ERROR", f"{parts[1]}: {exc}")
            return self.reply(500, {"Code": "ERROR", "Message": str(exc)})

    do_POST = do_GET


state = load_state()
log("INFO", f"Starting; cluster {CLUSTER} master {state['master']}; recoveries enabled: {state['recoveries_enabled']}")
threading.Thread(target=watch, daemon=True).start()
ThreadingHTTPServer(("", 3000), Handler).serve_forever()
