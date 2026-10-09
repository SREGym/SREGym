"""Private qualification repair using normal accessible database history and tools."""

import copy
import hashlib
import json
import os
import re
import selectors
import shlex
import shutil
import signal
import sqlite3
import subprocess
import tempfile
import threading
import time
from contextlib import ExitStack, suppress
from pathlib import Path
from uuid import UUID, uuid4

from sregym.generators.fault.regional_database_failover import service_name
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


def owned_worker_pods(app, namespace, *, core=None, request_timeout=None):
    from kubernetes.client import AppsV1Api

    core = core if core is not None else app._client().core_v1_api
    api = AppsV1Api(core.api_client)
    deployment = api.read_namespaced_deployment(
        "worker", namespace, _request_timeout=5 if request_timeout is None else request_timeout()
    )
    if not any(
        resource.kind == "Deployment"
        and resource.namespace == namespace
        and resource.name == "worker"
        and resource.uid == deployment.metadata.uid
        for resource in app.inventory.resources
    ):
        raise RuntimeError("Worker deployment was not captured as owned")
    replicas = api.list_namespaced_replica_set(
        namespace,
        label_selector="app.kubernetes.io/component=worker",
        _request_timeout=5 if request_timeout is None else request_timeout(),
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
        namespace,
        label_selector="app.kubernetes.io/component=worker",
        _request_timeout=5 if request_timeout is None else request_timeout(),
    ).items
    return tuple(
        pod
        for pod in pods
        if any(
            owner.kind == "ReplicaSet" and owner.controller and owner.uid in replica_uids
            for owner in (pod.metadata.owner_references or [])
        )
    )


def rewrite_worker_group_route(
    app, region_name, group_name, host, port, *, timeout=90, cancel=None, await_projection=True
):
    """UID-qualified normal config change, followed by all-worker mounted proof."""
    from kubernetes.client import ApiClient, CoreV1Api

    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not 0 < timeout <= 300
        or type(await_projection) is not bool
    ):
        raise ValueError("Worker route update requires a bounded deadline and explicit projection policy")
    deadline = time.monotonic() + timeout

    def remaining():
        if cancel is not None and cancel.is_set():
            raise RuntimeError("Normal worker route update cancelled")
        value = deadline - time.monotonic()
        if value <= 0:
            raise TimeoutError("Normal worker route update exceeded its deadline")
        return value

    def request_timeout():
        value = remaining()
        return min(5, int(value)) if value >= 1 else (value / 2, value / 2)

    namespace = next(region.namespace for region in app.regions if region.name == region_name)
    with ExitStack() as stack:
        core = app._client().core_v1_api
        if isinstance(core, CoreV1Api):
            configuration = copy.deepcopy(core.api_client.configuration)
            configuration.retries = 0
            core = CoreV1Api(stack.enter_context(ApiClient(configuration)))
        actual_namespace = core.read_namespace(namespace, _request_timeout=request_timeout())
        if not any(
            resource.kind == "Namespace"
            and resource.name == namespace
            and resource.uid == actual_namespace.metadata.uid
            for resource in app.inventory.resources
        ):
            raise RuntimeError("Worker route namespace ownership changed")
        if not owned_worker_pods(app, namespace, core=core, request_timeout=request_timeout):
            raise RuntimeError("No actual owned workers are available for route configuration")
        current = core.read_namespaced_config_map(
            "worker-database-routes", namespace, _request_timeout=request_timeout()
        )
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
                "data": dict(current.data) | {"database-routes.json": canonical(replacement)},
            },
            _request_timeout=request_timeout(),
        )
    remaining()
    if await_projection:
        app.await_worker_group_route(region_name, group_name, host, port, timeout_seconds=remaining(), cancel=cancel)


class RecoveredHistory:
    """Bounded on-disk merge of source rows, independent of expected receipts."""

    def __init__(self, path: Path, *, byte_budget=16 * 1024**3):
        if type(byte_budget) is not int or byte_budget < 4 * 1024**2:
            raise ValueError("Recovery merge requires a bounded budget of at least 4 MiB")
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path, self.byte_budget = path, byte_budget
        self.database = sqlite3.connect(path)
        os.chmod(path, 0o600)
        self.database.executescript(
            "PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL;"
            "CREATE TABLE IF NOT EXISTS history(event_id TEXT PRIMARY KEY,record_key TEXT NOT NULL,revision INTEGER NOT NULL,"
            "body TEXT NOT NULL,accepted_at TEXT NOT NULL,UNIQUE(record_key,revision));"
            "CREATE TABLE IF NOT EXISTS users(id TEXT PRIMARY KEY,body TEXT NOT NULL);"
            "CREATE TABLE IF NOT EXISTS retained(event_id TEXT PRIMARY KEY REFERENCES history(event_id));"
            "CREATE INDEX IF NOT EXISTS history_order ON history(accepted_at,event_id);"
        )
        page_size = self.database.execute("PRAGMA page_size").fetchone()[0]
        self.database.execute(f"PRAGMA max_page_count={byte_budget // (4 * page_size)}")
        self.database.execute("PRAGMA wal_autocheckpoint=64")
        self._uncommitted = 0

    def _check_storage(self, *, additional=0):
        used = sum(entry.stat().st_size for entry in self.path.parent.iterdir() if entry.is_file())
        if (
            used + additional > self.byte_budget - 1024**2
            or shutil.disk_usage(self.path.parent).free < self.byte_budget + 8 * 1024**3
        ):
            raise RuntimeError("Private recovery spool capacity is unavailable")

    def flush(self):
        self.database.commit()
        self._uncommitted = 0

    def _changed(self):
        self._uncommitted += 1
        if self._uncommitted >= 512:
            self.flush()

    def add_operation(self, row, *, retained=False):
        self._check_storage()
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
            if retained:
                self.database.execute("INSERT OR IGNORE INTO retained VALUES(?)", (operation.event_id,))
                self._changed()
            return
        try:
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
            if retained:
                self.database.execute("INSERT INTO retained VALUES(?)", (operation.event_id,))
            self._changed()
        except sqlite3.IntegrityError as exc:
            raise RuntimeError("Recovered histories disagree on a record revision") from exc

    def add_user(self, row):
        self._check_storage()
        if not re.fullmatch(r"[0-9a-f]{64}", row["token_sha256"]):
            raise ValueError("Recovered user credential digest is invalid")
        encoded = canonical({key: row[key] for key in ("id", "username", "token_sha256")})
        previous = self.database.execute("SELECT body FROM users WHERE id=?", (row["id"],)).fetchone()
        if previous and previous[0] != encoded:
            raise RuntimeError("Recovered user identities have conflicting credentials")
        self.database.execute("INSERT OR IGNORE INTO users VALUES(?,?)", (row["id"], encoded))
        self._changed()

    def write_stream(self, path, *, missing_only=False, deadline=None, cancelled=None):
        self.flush()

        def remaining():
            if cancelled is not None and cancelled.is_set():
                raise RuntimeError("Normal reference recovery cancelled")
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError("Normal reference recovery deadline expired")

        with path.open("w", encoding="utf-8") as output:
            os.chmod(path, 0o600)
            for (body,) in self.database.execute("SELECT body FROM users ORDER BY id"):
                remaining()
                line = canonical({"user": json.loads(body)}) + "\n"
                self._check_storage(additional=len(line.encode()))
                output.write(line)
                output.flush()
            # Modeled regions share the same host clock. Other clock models need explicit dependency ordering.
            query = "SELECT body FROM history"
            if missing_only:
                query += " WHERE NOT EXISTS(SELECT 1 FROM retained WHERE retained.event_id=history.event_id)"
            for (body,) in self.database.execute(query + " ORDER BY accepted_at,event_id"):
                remaining()
                line = canonical({"operation": json.loads(body)}) + "\n"
                self._check_storage(additional=len(line.encode()))
                output.write(line)
                output.flush()
        return path

    def close(self):
        try:
            self.flush()
        finally:
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
for line in sys.stdin:
    if len(line.encode())>262144: raise RuntimeError('Recovered operation exceeds capacity')
    item=json.loads(line)
    if 'user' in item:
        user=item['user']
        with storage.connection() as connection,connection.cursor() as cursor:
            cursor.execute('SELECT id,username,token_sha256 FROM users WHERE id=%s FOR UPDATE',(user['id'],))
            previous=cursor.fetchone()
            if previous:
                if previous!=user: raise RuntimeError('Recovered user identity conflicts with retained credentials')
                continue
            cursor.execute('INSERT INTO users(id,username,token_sha256) VALUES(%s,%s,%s)',
                           (user['id'],user['username'],user['token_sha256']))
            connection.commit()
    else:
        operation=item['operation'];actor=operation.pop('actor_id')
        storage.accept(Operation(**operation),actor)
print('Recovered ordinary journal replay completed')
"""


class DatabaseReferenceRepair:
    def __init__(self, app, private_dir: Path, *, cancel=None):
        self.app, self.private_dir = app, private_dir
        self.cancel = cancel if cancel is not None else threading.Event()

    def _save_diagnostics(self, stage, streams):
        """Retain bounded failure evidence only in the private recovery directory."""
        directory = self.private_dir / "diagnostics"
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if len(tuple(directory.iterdir())) >= 32:
            return
        path = directory / (stage + "-" + uuid4().hex + ".log")
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            for label, stream in streams:
                stream.seek(0)
                output.write(label.encode() + b"\n" + stream.read(65536) + b"\n")

    def _remaining(self, maximum=None):
        if self.cancel.is_set():
            raise RuntimeError("Normal reference recovery cancelled")
        capacity = getattr(self.app, "_capacity_monitor", None)
        if capacity is not None:
            capacity.assert_available()
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Normal reference recovery deadline expired")
        return remaining if maximum is None else min(maximum, remaining)

    @staticmethod
    def _reap(process):
        if os.name == "posix":
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGTERM)
        else:
            if process.poll() is not None:
                return
            process.terminate()
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            if os.name != "posix":
                process.kill()
        finally:
            if os.name == "posix":
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=1)

    def _run_process(self, command, *, stdin, capture_output=False, maximum=None):
        self._remaining()
        deadline = self._deadline if maximum is None else min(self._deadline, time.monotonic() + maximum)
        with tempfile.TemporaryFile() as diagnostics, tempfile.TemporaryFile() as output:
            process = subprocess.Popen(
                command,
                stdin=stdin,
                stdout=output if capture_output else subprocess.DEVNULL,
                stderr=diagnostics,
                start_new_session=os.name == "posix",
            )
            try:
                while process.poll() is None:
                    if diagnostics.seek(0, os.SEEK_END) > 65536 or output.seek(0, os.SEEK_END) > 1024**2:
                        raise RuntimeError("Normal recovery diagnostics exceed capacity")
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Normal recovery command deadline expired")
                    self.cancel.wait(min(self._remaining(0.1), deadline - time.monotonic()))
                self._remaining()
                if process.returncode:
                    self._save_diagnostics("command", (("stderr", diagnostics),))
                    raise RuntimeError("Normal recovery command failed; source recovery files remain retained")
                if output.seek(0, os.SEEK_END) > 1024**2:
                    raise RuntimeError("Normal recovery output exceeds capacity")
                output.seek(0)
                return output.read().decode("utf-8") if capture_output else None
            finally:
                self._reap(process)

    def _sql(self, member, query):
        from sregym.service.apps.codehub import CodeHub

        if isinstance(self.app, CodeHub):
            if not any(member in group.members for group in self.app.database_groups):
                raise ValueError("Database member is outside the captured deployment")
            self._assert_owner(member)
            namespace = next(region.namespace for region in self.app.regions if region.name == member.region)
            shell = (
                'MYSQL_PWD="$(cat /run/credentials/mysql-root-password)" exec mysql -uroot --batch --raw -e '
                + shlex.quote(query)
            )
            return self._run_process(
                [
                    "kubectl",
                    "-n",
                    namespace,
                    "exec",
                    service_name(member) + "-0",
                    "-c",
                    "mysql",
                    "--",
                    "sh",
                    "-c",
                    shell,
                ],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                maximum=self._remaining(30),
            )
        return self.app.mysql_command(member, query, timeout=self._remaining(30))

    def _sql_json(self, member, query):
        return [json.loads(line) for line in self._sql(member, query).splitlines() if line.startswith("{")]

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

    def _export(self, member, recovered, *, retained=False):
        """Stream fenced ordinary SQL once per table with bounded line buffers."""
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
            namespace = next(region.namespace for region in self.app.regions if region.name == member.region)
            sql = f"SELECT JSON_OBJECT({fields}) FROM codehub.{table} ORDER BY {identity};"
            shell = (
                'MYSQL_PWD="$(cat /run/credentials/mysql-root-password)" exec mysql -uroot --quick --batch --raw --skip-column-names -e '
                + shlex.quote(sql)
            )
            command = [
                "kubectl",
                "-n",
                namespace,
                "exec",
                service_name(member) + "-0",
                "-c",
                "mysql",
                "--",
                "sh",
                "-c",
                shell,
            ]
            for row in self._stream_rows(command):
                if str(UUID(row[identity])) != row[identity]:
                    raise RuntimeError("Recovered source has a noncanonical database identity")
                if table == "journal":
                    accept(row, retained=retained)
                else:
                    accept(row)
            recovered.flush()

    def _stream_rows(self, command):
        self._remaining()
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=os.name == "posix",
        )
        buffers = {"stdout": bytearray(), "stderr": bytearray()}
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ, "stdout")
                selector.register(process.stderr, selectors.EVENT_READ, "stderr")
                while selector.get_map():
                    for key, _ in selector.select(self._remaining(0.2)):
                        chunk = os.read(key.fd, 64 * 1024)
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        buffer = buffers[key.data]
                        buffer.extend(chunk)
                        if key.data == "stdout":
                            while (end := buffer.find(b"\n")) >= 0:
                                self._remaining()
                                if end > 262144:
                                    raise RuntimeError("Recovered operation exceeds capacity")
                                line = bytes(buffer[:end])
                                del buffer[: end + 1]
                                yield json.loads(line)
                        limit = 262144 if key.data == "stdout" else 65536
                        if len(buffer) > limit:
                            raise RuntimeError("Normal SQL recovery output exceeds capacity")
                if process.wait(timeout=self._remaining()) != 0:
                    raise RuntimeError("Normal source SQL export failed; source histories remain intact")
                if buffers["stdout"]:
                    raise RuntimeError("Normal SQL recovery export ended with a truncated row")
        finally:
            self._reap(process)
            process.stdout.close()
            process.stderr.close()

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

    def _reserve_copy(self, member):
        capacity = getattr(self.app, "_capacity_monitor", None)
        if capacity is None:
            return
        capacity.reserve_restore()
        namespace = next(region.namespace for region in self.app.regions if region.name == member.region)
        key = namespace + "/data-" + service_name(member) + "-0"
        allocations = capacity.latest.get("store_allocations", {})
        if key not in allocations:
            raise RuntimeError("Selected recovery source lacks its captured native allocation")
        # Destination allocation is still present in the absolute total. Reserve
        # one complete source copy conservatively, without charging other groups
        # or an already completed private recovery spool a second time.
        capacity.reserve_restore(additional_bytes=allocations[key] + 64 * 1024**2)

    def run(self, group_name="group-0", *, timeout=600):
        if type(timeout) not in {int, float} or not 0 < timeout <= 3600:
            raise ValueError("Reference repair requires a bounded positive total timeout")
        self._deadline = time.monotonic() + timeout
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
        capacity = getattr(self.app, "_capacity_monitor", None)
        spool_budget = {"small": 512 * 1024**2, "medium": 4 * 1024**3, "large": 16 * 1024**3}.get(
            getattr(getattr(self.app, "tier", None), "name", "small"), 512 * 1024**2
        )
        retained_bytes = sum(path.stat().st_size for path in self.private_dir.glob("*/*") if path.is_file())
        if retained_bytes + spool_budget > 4 * spool_budget:
            raise RuntimeError("Retained recovery attempts exceed their bounded storage")
        if capacity is not None:
            capacity.reserve_restore(additional_bytes=spool_budget)
        self._remaining()
        recovery_dir = self.private_dir / str(uuid4())
        recovery_dir.mkdir(mode=0o700)
        for member in group.members:
            self._assert_owner(member)
        recovered = RecoveredHistory(recovery_dir / "recovered.sqlite", byte_budget=spool_budget)
        try:
            # All application writes stay fenced; root's ordinary DBA connection can write with read_only ON.
            for member in sources:
                self._sql(member, "SET PERSIST read_only=ON; SET PERSIST super_read_only=ON;")
            for member in sources:
                self._export(member, recovered, retained=member == canonical_writer)
            path = recovered.write_stream(
                recovery_dir / "recovered.jsonl", missing_only=True, deadline=self._deadline, cancelled=self.cancel
            )
            self._stop_replica(canonical_writer)
            # Remove the obsolete upstream channel while retaining the entire
            # selected writer's GTID/binlog history. It must not resume the old
            # writer's stream or expose a stopped channel after restart.
            self._sql(canonical_writer, "RESET REPLICA ALL; SET PERSIST_ONLY skip_replica_start=ON;")
            self._sql(canonical_writer, "SET GLOBAL super_read_only=OFF; SET GLOBAL read_only=ON;")
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
                self._run_process(command, stdin=stream)
            for member in group.members:
                if member == canonical_writer:
                    continue
                self._assert_owner(member)
                self._stop_replica(member)
                self._reserve_copy(canonical_writer)
                self._sql(
                    member,
                    "SET GLOBAL super_read_only=OFF; SET GLOBAL read_only=ON; "
                    "RESET REPLICA ALL; RESET BINARY LOGS AND GTIDS; SET SESSION sql_log_bin=0; "
                    "DROP DATABASE codehub; CREATE DATABASE codehub CHARACTER SET utf8mb4; SET SESSION sql_log_bin=1;",
                )
                self._copy_database(canonical_writer, member, timeout=self._remaining())
                password = self.app._credentials["replication-password"]
                source_host, source_port = endpoints[member.name]
                self._sql(
                    member,
                    f"CHANGE REPLICATION SOURCE TO SOURCE_HOST='{source_host}', SOURCE_PORT={source_port}, "
                    f"SOURCE_USER='replication', SOURCE_PASSWORD='{password}', SOURCE_AUTO_POSITION=1, GET_SOURCE_PUBLIC_KEY=1, "
                    "SOURCE_CONNECT_RETRY=1, SOURCE_RETRY_COUNT=600; "
                    "START REPLICA; SET PERSIST read_only=ON; SET PERSIST super_read_only=ON; "
                    "SET PERSIST_ONLY skip_replica_start=OFF;",
                )
            rows = self._sql_json(canonical_writer, "SELECT JSON_OBJECT('gtid',@@GLOBAL.gtid_executed);")
            executed = rows[0]["gtid"] if rows else ""
            if not re.fullmatch(r"[0-9a-fA-F:,\-\n]+", executed):
                raise RuntimeError("Canonical database did not retain a valid GTID history")
            for member in group.members:
                if member != canonical_writer:
                    rows = self._sql_json(
                        member,
                        f"SELECT JSON_OBJECT('wait',WAIT_FOR_EXECUTED_GTID_SET('{executed}',{self._remaining(25):.3f}));",
                    )
                    if not rows or rows[0]["wait"] != 0:
                        raise RuntimeError("Rebuilt reader did not converge to the recovered history")
            for region in self.app.regions:
                self._remaining()
                self.app.set_writer_route(region.name, canonical_writer)
                self._restore_worker_route(region.namespace, group_name)
            self._sql(canonical_writer, "SET PERSIST super_read_only=OFF; SET PERSIST read_only=OFF;")
            return {
                "group": group_name,
                "canonical_writer": canonical_writer.name,
                "recovered_operations": recovered.database.execute("SELECT COUNT(*) FROM history").fetchone()[0],
            }
        finally:
            recovered.close()

    def _stop_replica(self, member):
        rows = self._sql_json(
            member,
            "SELECT JSON_OBJECT('channels',COUNT(*)) FROM performance_schema.replication_connection_configuration;",
        )
        if not rows:
            raise RuntimeError("Cannot observe ordinary replica channel configuration")
        if rows[0]["channels"]:
            self._sql(member, "STOP REPLICA;")

    def _copy_database(self, source, destination, *, timeout):
        def command(member, utility):
            namespace = next(region.namespace for region in self.app.regions if region.name == member.region)
            shell = f'MYSQL_PWD="$(cat /run/credentials/mysql-root-password)" exec {utility}'
            return [
                "kubectl",
                "-n",
                namespace,
                "exec",
                "-i",
                service_name(member) + "-0",
                "-c",
                "mysql",
                "--",
                "sh",
                "-c",
                shell,
            ]

        dump = command(source, "mysqldump -uroot --single-transaction --set-gtid-purged=ON codehub")
        restore = command(destination, "mysql -uroot codehub")
        self._remaining()
        deadline = min(self._deadline, time.monotonic() + timeout)
        processes = []
        with tempfile.TemporaryFile() as dump_error, tempfile.TemporaryFile() as restore_error:
            try:
                exporter = subprocess.Popen(
                    dump,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=dump_error,
                    start_new_session=os.name == "posix",
                )
                processes.append(exporter)
                importer = subprocess.Popen(
                    restore,
                    stdin=exporter.stdout,
                    stdout=subprocess.DEVNULL,
                    stderr=restore_error,
                    start_new_session=os.name == "posix",
                )
                processes.append(importer)
                exporter.stdout.close()
                while any(process.poll() is None for process in processes):
                    self._remaining()
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Normal database copy deadline expired")
                    if any(file.seek(0, os.SEEK_END) > 65536 for file in (dump_error, restore_error)):
                        raise RuntimeError("Normal database copy diagnostics exceed capacity")
                    if any(process.poll() not in (None, 0) for process in processes):
                        self._save_diagnostics("database-copy", (("export", dump_error), ("import", restore_error)))
                        raise RuntimeError("Normal database copy failed; source histories remain retained")
                    self.cancel.wait(min(0.1, deadline - time.monotonic()))
                self._remaining()
                if any(process.returncode for process in processes):
                    self._save_diagnostics("database-copy", (("export", dump_error), ("import", restore_error)))
                    raise RuntimeError("Normal database copy failed; source histories remain retained")
            finally:
                for process in reversed(processes):
                    self._reap(process)
                if processes and not processes[0].stdout.closed:
                    processes[0].stdout.close()

    def _restore_worker_route(self, namespace, group_name="group-0"):
        region = next(region for region in self.app.regions if region.namespace == namespace)
        group_index = group_name.removeprefix("group-")
        if not group_index.isdecimal():
            raise ValueError("Database group has no ordinary writer alias")
        rewrite_worker_group_route(
            self.app,
            region.name,
            group_name,
            f"mysql-g{group_index}-writer-route",
            3306,
            timeout=self._remaining(300),
            cancel=self.cancel,
        )


def url_host(member):
    from urllib.parse import urlsplit

    return urlsplit(member.origin).hostname
