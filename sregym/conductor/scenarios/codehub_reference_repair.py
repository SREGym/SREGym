"""Private qualification repair using normal accessible database history and tools."""

import hashlib
import json
import os
import re
import shlex
import sqlite3
import subprocess
import time
from pathlib import Path
from uuid import UUID, uuid4

from sregym.generators.fault.regional_database_failover import service_name, sql_json
from sregym.generators.workload.codehub import Operation, canonical


def worker_group_routes(routes, group_name, host, port):
    """Change one ordinary group's writer while preserving all other routes."""
    if type(routes) is not dict or not routes or any(type(route) is not dict for route in routes.values()):
        raise ValueError("Worker database routes must be a nonempty ordinary configuration")
    if (
        type(host) is not str
        or not re.fullmatch(r"[a-zA-Z0-9.-]{1,253}", host)
        or type(port) is not int
        or not 1 <= port <= 65535
    ):
        raise ValueError("Worker writer endpoint is invalid")
    copied = json.loads(canonical(routes))
    changed = 0
    for route in copied.values():
        if route.get("group", "group-0") == group_name:
            route.update(writer_host=host, port=port)
            changed += 1
    if not changed:
        raise RuntimeError("Selected worker group has no configured legitimate tenants")
    return copied


def owned_worker_pods(app, namespace):
    from kubernetes.client import AppsV1Api

    core = app._client().core_v1_api
    api = AppsV1Api(core.api_client)
    deployment = api.read_namespaced_deployment("worker", namespace, _request_timeout=5)
    if not any(
        resource.kind == "Deployment"
        and resource.namespace == namespace
        and resource.name == "worker"
        and resource.uid == deployment.metadata.uid
        for resource in app.inventory.resources
    ):
        raise RuntimeError("Worker deployment was not captured as owned")
    replicas = api.list_namespaced_replica_set(
        namespace, label_selector="app.kubernetes.io/component=worker", _request_timeout=5
    ).items
    replica_uids = {
        replica.metadata.uid
        for replica in replicas
        if any(
            owner.kind == "Deployment" and owner.controller and owner.uid == deployment.metadata.uid
            for owner in (replica.metadata.owner_references or [])
        )
    }
    pods = core.list_namespaced_pod(
        namespace, label_selector="app.kubernetes.io/component=worker", _request_timeout=5
    ).items
    return tuple(
        pod
        for pod in pods
        if any(
            owner.kind == "ReplicaSet" and owner.controller and owner.uid in replica_uids
            for owner in (pod.metadata.owner_references or [])
        )
    )


def rewrite_worker_group_route(app, region_name, group_name, host, port, *, timeout=90):
    """UID-qualified normal config change; mounted-file proof avoids restart of unaffected work."""
    namespace = next(region.namespace for region in app.regions if region.name == region_name)
    core = app._client().core_v1_api
    actual_namespace = core.read_namespace(namespace, _request_timeout=5)
    if not any(
        resource.kind == "Namespace" and resource.name == namespace and resource.uid == actual_namespace.metadata.uid
        for resource in app.inventory.resources
    ):
        raise RuntimeError("Worker route namespace ownership changed")
    if not owned_worker_pods(app, namespace):
        raise RuntimeError("No actual owned workers are available for route configuration")
    current = core.read_namespaced_config_map("worker-database-routes", namespace, _request_timeout=5)
    if not any(
        resource.kind == "ConfigMap"
        and resource.name == "worker-database-routes"
        and resource.namespace == namespace
        and resource.uid == current.metadata.uid
        for resource in app.inventory.resources
    ):
        raise RuntimeError("Worker route ConfigMap ownership changed")
    routes = json.loads(current.data["database-routes.json"])
    replacement = worker_group_routes(routes, group_name, host, port)
    core.patch_namespaced_config_map(
        "worker-database-routes",
        namespace,
        body={
            "metadata": {"resourceVersion": current.metadata.resource_version, "uid": current.metadata.uid},
            "data": {"database-routes.json": canonical(replacement)},
        },
        _request_timeout=5,
    )
    program = """import json,os,sys
from pathlib import Path
routes=json.loads(Path(os.environ['CODEHUB_DATABASE_ROUTES_FILE']).read_text());group,host,port=sys.argv[1:]
selected=[route for route in routes.values() if route.get('group','group-0')==group]
print(json.dumps({'applied':bool(selected) and all(route.get('writer_host')==host and int(route.get('port',3306))==int(port) for route in selected)}))
"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        applied = True
        pods = owned_worker_pods(app, namespace)
        if not pods:
            applied = False
        for pod in pods:
            command = f"kubectl -n {namespace} exec {pod.metadata.name} -c worker -- python -c {shlex.quote(program)} {group_name} {host} {port}"
            output = app._client().exec_command_checked(command, timeout=min(15, max(1, deadline - time.monotonic())))
            applied = applied and json.loads(output.strip().splitlines()[-1]).get("applied") is True
        if applied:
            return
        time.sleep(min(1, max(0, deadline - time.monotonic())))
    raise RuntimeError("Worker mounted routes did not observe the ordinary selected-group configuration")


class RecoveredHistory:
    """Bounded on-disk merge of source rows, independent of expected receipts."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.database = sqlite3.connect(path)
        os.chmod(path, 0o600)
        self.database.executescript(
            "PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL;"
            "CREATE TABLE IF NOT EXISTS history(event_id TEXT PRIMARY KEY,record_key TEXT NOT NULL,revision INTEGER NOT NULL,"
            "body TEXT NOT NULL,accepted_at TEXT NOT NULL,UNIQUE(record_key,revision));"
            "CREATE TABLE IF NOT EXISTS users(id TEXT PRIMARY KEY,body TEXT NOT NULL);"
        )

    def add_operation(self, row):
        operation = Operation(
            row["event_id"],
            row["tenant_id"],
            row["entity_id"],
            row["project_id"],
            row["client_revision"],
            row["kind"],
            canonical(row["payload"]),
            row["actor_id"],
        )
        encoded = canonical(operation.observed_row())
        if hashlib.sha256(encoded.encode()).hexdigest() != row["payload_sha256"]:
            raise RuntimeError("Recovered journal has an invalid operation digest")
        previous = self.database.execute("SELECT body FROM history WHERE event_id=?", (operation.event_id,)).fetchone()
        if previous:
            if previous[0] != encoded:
                raise RuntimeError("Recovered histories disagree on an event identity")
            return
        try:
            with self.database:
                self.database.execute(
                    "INSERT INTO history VALUES(?,?,?,?,?)",
                    (
                        operation.event_id,
                        operation.record_key,
                        operation.client_revision,
                        encoded,
                        row["accepted_at"],
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise RuntimeError("Recovered histories disagree on a record revision") from exc

    def add_user(self, row):
        if not re.fullmatch(r"[0-9a-f]{64}", row["token_sha256"]):
            raise ValueError("Recovered user credential digest is invalid")
        encoded = canonical({key: row[key] for key in ("id", "username", "token_sha256")})
        previous = self.database.execute("SELECT body FROM users WHERE id=?", (row["id"],)).fetchone()
        if previous and previous[0] != encoded:
            raise RuntimeError("Recovered user identities have conflicting credentials")
        with self.database:
            self.database.execute("INSERT OR IGNORE INTO users VALUES(?,?)", (row["id"], encoded))

    def write_stream(self, path):
        with path.open("w", encoding="utf-8") as output:
            os.chmod(path, 0o600)
            for (body,) in self.database.execute("SELECT body FROM users ORDER BY id"):
                output.write(canonical({"user": json.loads(body)}) + "\n")
            # Modeled regions share the same host clock. Other clock models need explicit dependency ordering.
            for (body,) in self.database.execute("SELECT body FROM history ORDER BY accepted_at,event_id"):
                output.write(canonical({"operation": json.loads(body)}) + "\n")
        return path

    def close(self):
        self.database.close()


REPLAY_PROGRAM = """
import json,sys
from dataclasses import replace
from pathlib import Path
from codehub.config import Settings
from codehub.domain import Operation
from codehub.storage.mysql import MySQLStorage
settings=replace(Settings.load(),mysql_host=sys.argv[1],mysql_user='root',mysql_read_host=None,
                 mysql_password=Path('/run/credentials/mysql-root-password').read_text().strip())
storage=MySQLStorage(settings)
tables=['builds','artifacts','outbox','journal','repository_refs','reviews','comments','issues','changes',
        'webhooks','entities','memberships','projects','organizations','users']
with storage.connection() as connection,connection.cursor() as cursor:
    for table in tables: cursor.execute('DELETE FROM '+table)
    connection.commit()
for line in sys.stdin:
    if len(line.encode())>262144: raise RuntimeError('Recovered operation exceeds capacity')
    item=json.loads(line)
    if 'user' in item:
        user=item['user']
        with storage.connection() as connection,connection.cursor() as cursor:
            cursor.execute('INSERT INTO users(id,username,token_sha256) VALUES(%s,%s,%s)',
                           (user['id'],user['username'],user['token_sha256']))
            connection.commit()
    else:
        operation=item['operation'];actor=operation.pop('actor_id')
        storage.accept(Operation(**operation),actor)
print('Recovered ordinary journal replay completed')
"""


class DatabaseReferenceRepair:
    def __init__(self, app, private_dir: Path):
        self.app, self.private_dir = app, private_dir

    def _replication_endpoint(self, source_region, member):
        resolver = getattr(self.app, "normal_connection_endpoint", None)
        if not callable(resolver):
            raise RuntimeError("Normal owned database endpoint resolution is required before recovery")
        endpoint = resolver(source_region, member)
        if (
            type(endpoint) is not tuple
            or len(endpoint) != 2
            or type(endpoint[0]) is not str
            or not re.fullmatch(r"[a-zA-Z0-9.-]{1,253}", endpoint[0])
            or type(endpoint[1]) is not int
            or endpoint[1] != 3306
        ):
            raise ValueError("Recovery replication requires an ordinary owned SQL service endpoint")
        return endpoint

    def _export(self, member, recovered):
        for table, identity, fields, accept in (
            (
                "journal",
                "event_id",
                "'event_id',event_id,'tenant_id',tenant_id,'entity_id',entity_id,'project_id',project_id,"
                "'client_revision',client_revision,'kind',kind,'payload',payload,'actor_id',actor_id,"
                "'payload_sha256',payload_sha256,'accepted_at',DATE_FORMAT(accepted_at,'%Y-%m-%d %H:%i:%s.%f')",
                recovered.add_operation,
            ),
            ("users", "id", "'id',id,'username',username,'token_sha256',token_sha256", recovered.add_user),
        ):
            after = ""
            while True:
                rows = sql_json(
                    self.app,
                    member,
                    f"SELECT JSON_OBJECT({fields}) FROM codehub.{table} WHERE {identity}>'{after}' ORDER BY {identity} LIMIT 100;",
                )
                if not rows:
                    break
                for row in rows:
                    accept(row)
                after = rows[-1][identity]
                if str(UUID(after)) != after:
                    raise RuntimeError("Recovered source has a noncanonical database identity")

    def _assert_owner(self, member):
        namespace = next(region.namespace for region in self.app.regions if region.name == member.region)
        core = self.app._client().core_v1_api
        actual_namespace = core.read_namespace(namespace, _request_timeout=5)
        if not any(
            resource.kind == "Namespace"
            and resource.name == namespace
            and resource.uid == actual_namespace.metadata.uid
            for resource in self.app.inventory.resources
        ):
            raise RuntimeError("Recovery namespace ownership changed")
        from kubernetes.client import AppsV1Api

        stateful = AppsV1Api(core.api_client).read_namespaced_stateful_set(
            service_name(member), namespace, _request_timeout=5
        )
        if not any(
            resource.kind == "StatefulSet"
            and resource.name == service_name(member)
            and resource.namespace == namespace
            and resource.uid == stateful.metadata.uid
            for resource in self.app.inventory.resources
        ):
            raise RuntimeError("Recovery database ownership changed")

    def run(self, group_name="group-0", *, timeout=600):
        group = next(group for group in self.app.database_groups if group.name == group_name)
        if group_name != "group-0":
            raise RuntimeError("Additional-group route/promotion qualification is required before reference repair")
        sources = [member for member in group.members if member.role in {"writer", "candidate"}]
        canonical_writer = next(member for member in sources if member.role == "candidate")
        # Resolve ordinary paths before fencing or rebuilding any database. Remote
        # source connections keep the installed measured links after recovery.
        endpoints = {
            member.name: self._replication_endpoint(member.region, canonical_writer)
            for member in group.members
            if member != canonical_writer
        }
        self.private_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        recovery_dir = self.private_dir / str(uuid4())
        recovery_dir.mkdir(mode=0o700)
        for member in group.members:
            self._assert_owner(member)
        recovered = RecoveredHistory(recovery_dir / "recovered.sqlite")
        try:
            # All application writes stay fenced; root's ordinary DBA connection can write with read_only ON.
            for member in sources:
                self.app.mysql_command(member, "SET GLOBAL super_read_only=ON; SET GLOBAL read_only=ON;")
            for member in sources:
                self._export(member, recovered)
            path = recovered.write_stream(recovery_dir / "recovered.jsonl")
            self._stop_replica(canonical_writer)
            self.app.mysql_command(canonical_writer, "SET GLOBAL super_read_only=OFF; SET GLOBAL read_only=ON;")
            namespace = next(region.namespace for region in self.app.regions if region.name == canonical_writer.region)
            host = url_host(canonical_writer)
            command = [
                "kubectl",
                "-n",
                namespace,
                "exec",
                "-i",
                "deployment/api",
                "--",
                "python",
                "-c",
                REPLAY_PROGRAM,
                host,
            ]
            # Stream a normal recovery file; never buffer the full large history in the API process.
            with path.open("r", encoding="utf-8") as stream:
                result = subprocess.run(command, stdin=stream, capture_output=True, timeout=timeout, text=True)
                if result.returncode:
                    raise RuntimeError("Normal DBA journal replay failed; source recovery files remain retained")
            for member in group.members:
                if member == canonical_writer:
                    continue
                self._assert_owner(member)
                self._stop_replica(member)
                self.app.mysql_command(
                    member,
                    "SET GLOBAL super_read_only=OFF; SET GLOBAL read_only=ON; "
                    "RESET REPLICA ALL; RESET BINARY LOGS AND GTIDS; DROP DATABASE codehub; CREATE DATABASE codehub CHARACTER SET utf8mb4;",
                )
                self._copy_database(canonical_writer, member, timeout=timeout)
                password = self.app._credentials["replication-password"]
                source_host, source_port = endpoints[member.name]
                self.app.mysql_command(
                    member,
                    f"CHANGE REPLICATION SOURCE TO SOURCE_HOST='{source_host}', SOURCE_PORT={source_port}, "
                    f"SOURCE_USER='replication', SOURCE_PASSWORD='{password}', SOURCE_AUTO_POSITION=1, GET_SOURCE_PUBLIC_KEY=1; "
                    "START REPLICA; SET GLOBAL super_read_only=ON;",
                )
            rows = sql_json(self.app, canonical_writer, "SELECT JSON_OBJECT('gtid',@@GLOBAL.gtid_executed);")
            executed = rows[0]["gtid"] if rows else ""
            if not re.fullmatch(r"[0-9a-fA-F:,\-\n]+", executed):
                raise RuntimeError("Canonical database did not retain a valid GTID history")
            for member in group.members:
                if member != canonical_writer:
                    rows = sql_json(
                        self.app, member, f"SELECT JSON_OBJECT('wait',WAIT_FOR_EXECUTED_GTID_SET('{executed}',30));"
                    )
                    if not rows or rows[0]["wait"] != 0:
                        raise RuntimeError("Rebuilt reader did not converge to the recovered history")
            for region in self.app.regions:
                self.app.set_writer_route(region.name, canonical_writer)
                self._restore_worker_route(region.namespace, group_name)
            self.app.mysql_command(canonical_writer, "SET GLOBAL super_read_only=OFF; SET GLOBAL read_only=OFF;")
            return {
                "group": group_name,
                "canonical_writer": canonical_writer.name,
                "recovered_operations": recovered.database.execute("SELECT COUNT(*) FROM history").fetchone()[0],
            }
        finally:
            recovered.close()

    def _stop_replica(self, member):
        rows = sql_json(
            self.app,
            member,
            "SELECT JSON_OBJECT('channels',COUNT(*)) FROM performance_schema.replication_connection_configuration;",
        )
        if not rows:
            raise RuntimeError("Cannot observe ordinary replica channel configuration")
        if rows[0]["channels"]:
            self.app.mysql_command(member, "STOP REPLICA;")

    def _copy_database(self, source, destination, *, timeout):
        def command(member, utility):
            namespace = next(region.namespace for region in self.app.regions if region.name == member.region)
            shell = f'MYSQL_PWD="$(cat /run/credentials/mysql-root-password)" exec {utility}'
            return f"kubectl -n {namespace} exec -i {service_name(member)}-0 -c mysql -- sh -c {shlex.quote(shell)}"

        dump = command(source, "mysqldump -uroot --single-transaction --set-gtid-purged=ON codehub")
        restore = command(destination, "mysql -uroot")
        self.app._client().exec_command_checked(
            f"bash -o pipefail -c {shlex.quote(dump + ' | ' + restore)}", timeout=timeout
        )

    def _restore_worker_route(self, namespace, group_name="group-0"):
        region = next(region for region in self.app.regions if region.namespace == namespace)
        group_index = group_name.removeprefix("group-")
        if not group_index.isdecimal():
            raise ValueError("Database group has no ordinary writer alias")
        rewrite_worker_group_route(self.app, region.name, group_name, f"mysql-g{group_index}-writer-route", 3306)


def url_host(member):
    from urllib.parse import urlsplit

    return urlsplit(member.origin).hostname
