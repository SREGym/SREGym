"""Verifier-container recovery checks using independent SQL and business reads.

No workload shell, host evaluation fallback, or server-reported verdict is used.
All runtime clients/processes are local to evaluation and leave the snapshot as
immutable endpoint/credential/receipt DTOs.
"""

import hashlib
import io
import json
import logging
import math
import os
import re
import secrets
import signal
import socket
import subprocess
import sys
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager, suppress
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory, TemporaryFile
from types import MappingProxyType
from uuid import UUID, uuid4

import httpx

from sregym.conductor.oracles.base import Oracle
from sregym.conductor.oracles.codehub_state import (
    DOMAIN_TABLES,
    KINDS,
    SEARCH_TYPES,
    EffectReceiptCut,
    ProtectedReceiptCut,
    ProtectedState,
    StateMismatch,
    canonical,
    digest,
    effect_identity,
)
from sregym.conductor.oracles.failure import FailureClass
from sregym.conductor.scenarios.codehub_key_inventory import register_cut_codec, validate_cut_snapshot_budget

register_cut_codec()

ENTITY_BATCH_SIZE = 128
BUSINESS_REPLICA_WORKERS = 8
BATCH_RESPONSE_BYTES = 40 * 1024**2
BATCH_DOCUMENT_BYTES = 256 * 1024


class EvidenceUnavailable(RuntimeError):
    """The trusted receipt owner cannot establish a complete mutation history."""


class VerificationCapacityUnavailable(EvidenceUnavailable):
    """A kernel-observed private verifier limit prevented an observation."""

    def __init__(self, message, *, resource="processes"):
        super().__init__(message)
        self.resource = resource


def _check_process_capacity():
    if os.environ.get("SREGYM_VERIFIER_CONTAINER") == "1":
        try:
            payload = Path("/sys/fs/cgroup/pids.events").read_text()
            if len(payload) <= 8192:
                events = dict(line.split() for line in payload.splitlines())
                if int(events.get("max", "0")) > 0:
                    raise VerificationCapacityUnavailable("Private verifier process capacity was exhausted")
        except (OSError, ValueError):
            pass


def _owned_command_failure(message):
    _check_process_capacity()
    raise RuntimeError(message)


@contextmanager
def _observation_phase(name, *, operations=None, group=None, member=None, observation=None, replicas=None):
    """Bounded private timing evidence for calibration, never a grading input."""
    started, completed = time.monotonic(), False
    try:
        yield
        completed = True
    finally:
        if os.environ.get("SREGYM_VERIFIER_CONTAINER") == "1":
            print(
                json.dumps(
                    {
                        "event": "recovery_observation_phase",
                        "phase": name,
                        "seconds": round(time.monotonic() - started, 3),
                        "completed": completed,
                        "operations": operations,
                        "group": group,
                        "member": member,
                        "observation": observation,
                        "replicas": replicas,
                    },
                    sort_keys=True,
                ),
                file=sys.stderr,
                flush=True,
            )


def _label(value):
    if type(value) is not str or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", value):
        raise ValueError("Endpoint names must be DNS labels")


def _uuid(value):
    if type(value) is not str or str(UUID(value)) != value:
        raise ValueError("Expected canonical UUID")


def _sha(value, length=64):
    if type(value) is not str or not re.fullmatch(rf"[0-9a-f]{{{length}}}", value):
        raise ValueError("Expected lowercase content digest")


@dataclass(frozen=True)
class SQLTarget:
    group: str
    region: str
    namespace: str
    service: str
    database: str
    user: str
    password: str = field(repr=False)
    port: int = 3306

    def __post_init__(self):
        for name in ("group", "region", "namespace", "service"):
            _label(getattr(self, name))
        for name in ("database", "user"):
            if type(getattr(self, name)) is not str or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", getattr(self, name)):
                raise ValueError("SQL database and account identifiers are invalid")
        if (
            type(self.password) is not str
            or not self.password
            or type(self.port) is not int
            or not 1 <= self.port <= 65535
        ):
            raise ValueError("SQL targets need credentials and a valid port")


@dataclass(frozen=True)
class HTTPServiceTarget:
    region: str
    namespace: str
    service: str
    port: int = 8080
    resource_kind: str = "service"
    expected_replicas: int | None = None
    scheme: str = "http"
    ca_certificate: str | None = field(default=None, repr=False)
    expected_uid: str | None = None

    def __post_init__(self):
        for name in ("region", "namespace", "service"):
            _label(getattr(self, name))
        if type(self.port) is not int or not 1 <= self.port <= 65535:
            raise ValueError("HTTP target port is invalid")
        if self.resource_kind not in {"service", "pod", "deployment", "statefulset"}:
            raise ValueError("HTTP target resource kind is invalid")
        if self.expected_replicas is not None and (
            type(self.expected_replicas) is not int or not 1 <= self.expected_replicas <= 64
        ):
            raise ValueError("HTTP replica inventory must be bounded between one and 64")
        if self.resource_kind in {"deployment", "statefulset"} and self.expected_replicas is None:
            raise ValueError("Grouped HTTP targets need an exact declared replica count")
        if self.resource_kind in {"service", "pod"} and self.expected_replicas not in {None, 1}:
            raise ValueError("A shared service cannot represent multiple replica observations")
        if self.expected_uid is not None and (
            type(self.expected_uid) is not str or not 1 <= len(self.expected_uid) <= 128
        ):
            raise ValueError("Captured controller identity must be bounded")
        if self.scheme not in {"http", "https"} or (
            self.scheme == "https"
            and (type(self.ca_certificate) is not str or "-----BEGIN CERTIFICATE-----" not in self.ca_certificate)
        ):
            raise ValueError("HTTPS observations require their independently retained CA certificate")
        if self.scheme == "http" and self.ca_certificate is not None:
            raise ValueError("HTTP observations cannot declare an unused TLS trust root")


@dataclass(frozen=True)
class ProcessTarget:
    region: str
    namespace: str
    name: str
    kind: str
    replicas: int
    uid: str

    def __post_init__(self):
        for value in (self.region, self.namespace, self.name):
            _label(value)
        if (
            self.kind not in {"deployment", "statefulset"}
            or type(self.replicas) is not int
            or not 1 <= self.replicas <= 64
        ):
            raise ValueError("Required process inventory must specify a bounded owned controller")
        if type(self.uid) is not str or not 1 <= len(self.uid) <= 128:
            raise ValueError("Required process inventory lacks captured controller identity")


@dataclass(frozen=True)
class DeliveryObserverTarget:
    read_url: str
    token: str = field(repr=False)
    transport: str = "http"

    def __post_init__(self):
        from urllib.parse import urlsplit

        if self.transport == "private_pipe":
            if self.read_url != "" or self.token != "":
                raise ValueError("Private receipt pipes carry no HTTP address or credentials")
            return
        if self.transport != "http":
            raise ValueError("Independent receipt transport is invalid")
        parsed = urlsplit(self.read_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Observer endpoint must be a credential-free private read URL")
        if type(self.token) is not str or len(self.token) < 32:
            raise ValueError("Observer read credentials must be independently generated")


@dataclass(frozen=True)
class WebhookDestination:
    id: str
    url: str
    events: tuple[str, ...]

    def __post_init__(self):
        from urllib.parse import urlsplit

        _uuid(self.id)
        parsed = urlsplit(self.url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("Expected normal credential-free webhook destination")
        if (
            type(self.events) is not tuple
            or not self.events
            or any(type(kind) is not str or kind not in KINDS for kind in self.events)
            or len(set(self.events)) != len(self.events)
        ):
            raise ValueError("Webhook events must be an immutable nonempty tuple")


@dataclass(frozen=True)
class FreshAPIChallenge:
    group: str
    tenant_id: str
    project_id: str
    actor_id: str
    token: str = field(repr=False)
    webhooks: tuple[WebhookDestination, ...] = ()

    def __post_init__(self):
        _label(self.group)
        for value in (self.tenant_id, self.project_id, self.actor_id):
            _uuid(value)
        if (
            type(self.token) is not str
            or len(self.token) < 32
            or type(self.webhooks) is not tuple
            or any(type(item) is not WebhookDestination for item in self.webhooks)
        ):
            raise ValueError("Fresh challenges require immutable ordinary customer credentials and subscriptions")


@dataclass(frozen=True)
class GitRefExpectation:
    ref: str
    commit_sha: str

    def __post_init__(self):
        if (
            type(self.ref) is not str
            or not self.ref.startswith("refs/heads/")
            or len(self.ref) > 200
            or re.search(r"[\s~^:?*\[\x00-\x1f]", self.ref)
            or any(value in self.ref for value in ("..", "@{", "\\", "//"))
        ):
            raise ValueError("Expected a normal Git branch ref")
        _sha(self.commit_sha, 40)


@dataclass(frozen=True)
class GitFileExpectation:
    commit_sha: str
    path: str
    sha256: str

    def __post_init__(self):
        _sha(self.commit_sha, 40)
        _sha(self.sha256)
        if type(self.path) is not str or len(self.path) > 2048:
            raise ValueError("Expected bounded repository-relative file path")
        path = PurePosixPath(self.path)
        if path.is_absolute() or ".." in path.parts or not path.parts or "\x00" in self.path:
            raise ValueError("Expected bounded repository-relative file path")


@dataclass(frozen=True)
class GitBundleExpectation:
    commit_sha: str
    sha256: str

    def __post_init__(self):
        _sha(self.commit_sha, 40)
        _sha(self.sha256)


@dataclass(frozen=True)
class GitProjectExpectation:
    project_id: str
    token: str = field(repr=False)
    refs: tuple[GitRefExpectation, ...]
    files: tuple[GitFileExpectation, ...]
    bundles: tuple[GitBundleExpectation, ...]

    def __post_init__(self):
        _uuid(self.project_id)
        for name, cls in (
            ("refs", GitRefExpectation),
            ("files", GitFileExpectation),
            ("bundles", GitBundleExpectation),
        ):
            entries = getattr(self, name)
            if type(entries) is not tuple or not entries or any(type(item) is not cls for item in entries):
                raise ValueError(f"{name} must be an immutable nonempty tuple")
        if type(self.token) is not str or len(self.token) < 32:
            raise ValueError("Git verification needs ordinary customer credentials")
        for entries in (
            tuple(value.ref for value in self.refs),
            tuple((value.commit_sha, value.path) for value in self.files),
            tuple(value.commit_sha for value in self.bundles),
        ):
            if len(set(entries)) != len(entries):
                raise ValueError("Git source expectations cannot silently replace duplicate identities")


@dataclass(frozen=True)
class RecoveryOutcomePlan:
    api_targets: tuple[HTTPServiceTarget, ...]
    search_targets: tuple[HTTPServiceTarget, ...]
    repository_targets: tuple[HTTPServiceTarget, ...]
    projects: tuple[GitProjectExpectation, ...]
    fresh_challenges: tuple[FreshAPIChallenge, ...]
    observer: DeliveryObserverTarget
    service_token: str = field(repr=False)
    gateway_targets: tuple[HTTPServiceTarget, ...] = ()
    process_targets: tuple[ProcessTarget, ...] = ()
    require_traffic: bool = False

    def __post_init__(self):
        for name, cls in (
            ("api_targets", HTTPServiceTarget),
            ("search_targets", HTTPServiceTarget),
            ("repository_targets", HTTPServiceTarget),
            ("projects", GitProjectExpectation),
            ("fresh_challenges", FreshAPIChallenge),
        ):
            entries = getattr(self, name)
            if type(entries) is not tuple or not entries or any(type(item) is not cls for item in entries):
                raise ValueError(f"{name} must provide immutable real business observation inventory")
        if (
            type(self.observer) is not DeliveryObserverTarget
            or type(self.service_token) is not str
            or len(self.service_token) < 32
        ):
            raise ValueError("Outcome observation needs private read-only observer and normal service credentials")
        if len({project.project_id for project in self.projects}) != len(self.projects):
            raise ValueError("Outcome project identities must be unique")
        if type(self.gateway_targets) is not tuple or any(
            type(t) is not HTTPServiceTarget for t in self.gateway_targets
        ):
            raise ValueError("Gateway observations must be immutable typed targets")
        if (
            type(self.require_traffic) is not bool
            or type(self.process_targets) is not tuple
            or any(type(target) is not ProcessTarget for target in self.process_targets)
        ):
            raise ValueError("Required continuing work must use immutable private process targets")
        if self.require_traffic and not self.process_targets:
            raise ValueError("Continuing customer work requires complete private process inventory")
        if self.process_targets:
            regions = {target.region for target in self.api_targets}
            for region in regions:
                targets = [target for target in self.process_targets if target.region == region]
                if len(targets) != 4 or {target.name for target in targets} != {
                    "worker",
                    "queue",
                    "delivery",
                    "topology",
                }:
                    raise ValueError("Every region requires complete worker, queue, delivery and topology inventory")
            if {target.region for target in self.process_targets} != regions:
                raise ValueError("Required processes differ from declared regions")
        if self.gateway_targets and {t.region for t in self.gateway_targets} != {t.region for t in self.api_targets}:
            raise ValueError("Customer gateways must cover every declared API region")


@contextmanager
def port_forward(namespace, service, remote_port, *, deadline, resource_kind="service"):
    _label(namespace)
    _label(service)
    if resource_kind not in {"service", "pod", "deployment", "statefulset"}:
        raise ValueError("Port-forward resource kind is invalid")
    """An owned subprocess with bounded startup and teardown, never a pod shell."""
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        local_port = reservation.getsockname()[1]
    with TemporaryFile() as diagnostics:
        process = subprocess.Popen(
            [
                "kubectl",
                "--request-timeout=10s",
                "-n",
                namespace,
                "port-forward",
                "--address=127.0.0.1",
                f"{resource_kind}/{service}",
                f"{local_port}:{remote_port}",
            ],
            stdin=subprocess.DEVNULL,
            stdout=diagnostics,
            stderr=diagnostics,
            env={**os.environ, "GOMAXPROCS": "2"},
        )
        try:
            startup_end = min(deadline, time.monotonic() + 10)
            while time.monotonic() < startup_end:
                if process.poll() is not None:
                    diagnostics.seek(0)
                    detail = diagnostics.read(8192).decode("utf-8", errors="replace")
                    _owned_command_failure(
                        f"Owned service port-forward exited before connecting: {namespace}/{service}: {detail}"
                    )
                try:
                    with socket.create_connection(("127.0.0.1", local_port), timeout=0.2):
                        break
                except OSError:
                    time.sleep(0.05)
            else:
                raise TimeoutError("Owned service port-forward startup deadline exceeded")
            yield local_port
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)


def _kube_json(namespace, arguments, *, deadline):
    remaining = min(10, deadline - time.monotonic())
    if remaining <= 0:
        raise TimeoutError("Replica inventory observation deadline exceeded")
    with TemporaryFile() as output, TemporaryFile() as errors:
        result = subprocess.run(
            ["kubectl", "--request-timeout=10s", "-n", namespace, "get", *arguments, "-o", "json"],
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=errors,
            timeout=remaining,
        )
        if result.returncode or output.tell() > 4 * 1024 * 1024 or errors.tell() > 1024 * 1024:
            _owned_command_failure("Replica inventory API observation failed")
        output.seek(0)
        return json.load(output)


def _selector(selector):
    if type(selector) is not dict or set(selector) - {"matchLabels", "matchExpressions"}:
        raise StateMismatch("regional_replica_inventory_mismatch")
    labels, expressions = selector.get("matchLabels", {}), selector.get("matchExpressions", [])
    if type(labels) is not dict or type(expressions) is not list or len(labels) + len(expressions) > 64:
        raise StateMismatch("regional_replica_inventory_mismatch")
    terms = []
    for key, value in sorted(labels.items()):
        if (
            type(key) is not str
            or type(value) is not str
            or not re.fullmatch(r"[A-Za-z0-9./_-]{1,253}", key)
            or not re.fullmatch(r"[A-Za-z0-9._-]{0,63}", value)
        ):
            raise StateMismatch("regional_replica_inventory_mismatch")
        terms.append(f"{key}={value}")
    for expression in expressions:
        if type(expression) is not dict or set(expression) - {"key", "operator", "values"}:
            raise StateMismatch("regional_replica_inventory_mismatch")
        key, operator, values = expression.get("key"), expression.get("operator"), expression.get("values", [])
        if (
            type(key) is not str
            or not re.fullmatch(r"[A-Za-z0-9./_-]{1,253}", key)
            or type(values) is not list
            or len(values) > 64
            or any(type(value) is not str or not re.fullmatch(r"[A-Za-z0-9._-]{0,63}", value) for value in values)
        ):
            raise StateMismatch("regional_replica_inventory_mismatch")
        if operator in {"In", "NotIn"} and values:
            terms.append(f"{key} {'in' if operator == 'In' else 'notin'} ({','.join(values)})")
        elif operator in {"Exists", "DoesNotExist"} and not values:
            terms.append(key if operator == "Exists" else f"!{key}")
        else:
            raise StateMismatch("regional_replica_inventory_mismatch")
    if not terms:
        raise StateMismatch("regional_replica_inventory_mismatch")
    return ",".join(terms)


def _owned(metadata, uid, kind):
    references = metadata.get("ownerReferences", [])
    return type(references) is list and any(
        type(value) is dict
        and value.get("uid") == uid
        and value.get("kind") == kind
        and value.get("controller") is True
        for value in references
    )


def _ready_pod(pod):
    if type(pod) is not dict or type(pod.get("metadata")) is not dict or type(pod.get("status")) is not dict:
        return False
    metadata, status = pod["metadata"], pod["status"]
    return (
        not metadata.get("deletionTimestamp")
        and status.get("phase") == "Running"
        and any(
            type(value) is dict and value.get("type") == "Ready" and value.get("status") == "True"
            for value in status.get("conditions", [])
        )
    )


def resolve_http_replicas(target, *, deadline):
    """Discover current owned ready pods through the ordinary Kubernetes API."""
    if target.resource_kind == "service":
        # Compatibility for callers with a qualified singleton service. Grouped
        # production inventories use controllers and observe each actual pod.
        return ((target, f"service:{target.namespace}/{target.service}"),)
    resource = _kube_json(target.namespace, [f"{target.resource_kind}/{target.service}"], deadline=deadline)
    if type(resource) is not dict or type(resource.get("metadata")) is not dict:
        raise StateMismatch("regional_replica_inventory_mismatch")
    uid = resource["metadata"].get("uid")
    if type(uid) is not str or not uid or (target.expected_uid is not None and uid != target.expected_uid):
        raise StateMismatch("regional_replica_inventory_mismatch")
    if target.resource_kind == "pod":
        if resource["metadata"].get("name") != target.service or not _ready_pod(resource):
            raise StateMismatch("regional_replica_inventory_mismatch")
        return ((target, uid),)
    spec, status = resource.get("spec", {}), resource.get("status", {})
    generation = resource["metadata"].get("generation")
    if (
        type(spec) is not dict
        or type(status) is not dict
        or type(spec.get("replicas")) is not int
        or spec["replicas"] != target.expected_replicas
        or type(generation) is not int
        or type(status.get("observedGeneration")) is not int
        or status["observedGeneration"] < generation
    ):
        raise StateMismatch("regional_replica_inventory_mismatch")
    selector = _selector(spec.get("selector"))
    response = _kube_json(target.namespace, ["pods", "-l", selector], deadline=deadline)
    if type(response) is not dict or type(response.get("items")) is not list or len(response["items"]) > 256:
        raise StateMismatch("regional_replica_inventory_mismatch")
    if target.resource_kind == "deployment":
        sets = _kube_json(target.namespace, ["replicasets", "-l", selector], deadline=deadline)
        if type(sets) is not dict or type(sets.get("items")) is not list or len(sets["items"]) > 256:
            raise StateMismatch("regional_replica_inventory_mismatch")
        owners = {
            value["metadata"].get("uid")
            for value in sets["items"]
            if type(value) is dict
            and type(value.get("metadata")) is dict
            and _owned(value["metadata"], uid, "Deployment")
        }
        owners.discard(None)
        kind = "ReplicaSet"
    else:
        owners, kind = {uid}, "StatefulSet"
    observed = []
    for pod in response["items"]:
        if not _ready_pod(pod) or not any(_owned(pod["metadata"], owner, kind) for owner in owners):
            continue
        name, pod_uid = pod["metadata"].get("name"), pod["metadata"].get("uid")
        _label(name)
        if type(pod_uid) is not str or not pod_uid:
            raise StateMismatch("regional_replica_inventory_mismatch")
        observed.append((replace(target, service=name, resource_kind="pod", expected_replicas=None), pod_uid))
    if len(observed) != target.expected_replicas or len({value[1] for value in observed}) != len(observed):
        raise StateMismatch("regional_replica_inventory_mismatch")
    return tuple(sorted(observed, key=lambda item: item[0].service))


class SQLObservation:
    def __init__(self, target, *, deadline):
        self.target, self.deadline = target, deadline

    @contextmanager
    def connection(self):
        import pymysql

        with port_forward(
            self.target.namespace, self.target.service, self.target.port, deadline=self.deadline
        ) as local:
            connection = pymysql.connect(
                host="127.0.0.1",
                port=local,
                database=self.target.database,
                user=self.target.user,
                password=self.target.password,
                charset="utf8mb4",
                connect_timeout=5,
                read_timeout=15,
                write_timeout=5,
                autocommit=False,
                cursorclass=pymysql.cursors.SSDictCursor,
            )
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ")
                    cursor.execute("START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY")
                yield connection
            finally:
                try:
                    connection.rollback()
                except pymysql.MySQLError:
                    pass  # Read-only teardown must not hide the original observation error.
                finally:
                    connection.close()

    def rows(self, connection, table, *, record_keys=None):
        columns = {
            "journal": "event_id,record_key,tenant_id,entity_id,project_id,client_revision,kind,CASE WHEN OCTET_LENGTH(payload)<=131072 THEN payload ELSE NULL END AS payload,actor_id,payload_sha256",
            "entities": "tenant_id,id,project_id,entity_type,revision,CASE WHEN OCTET_LENGTH(document)<=131072 THEN document ELSE NULL END AS document",
            "outbox": "effect_id,event_id,effect_kind,destination,CASE WHEN OCTET_LENGTH(payload)<=131072 THEN payload ELSE NULL END AS payload,state,attempts",
            "builds": "effect_id,event_id,project_id,commit_sha,artifact_sha256,file_count",
        }
        if table not in columns and table not in DOMAIN_TABLES.values():
            raise ValueError("SQL observation table is not allowlisted")
        parameters, clause = (), ""
        if record_keys is not None:
            if type(record_keys) is not tuple or not 1 <= len(record_keys) <= 1024:
                raise ValueError("Recent SQL scope must contain bounded private record identities")
            pairs = tuple(tuple(key.split("/")) for key in record_keys)
            for tenant, entity in pairs:
                _uuid(tenant)
                _uuid(entity)
            placeholders = ",".join("(%s,%s)" for _ in pairs)
            flattened = tuple(value for pair in pairs for value in pair)
            journal_scope = (
                f"(tenant_id,entity_id) IN ({placeholders}) OR record_key IN ({','.join('%s' for _ in pairs)})"
            )
            if table == "journal":
                clause, parameters = " WHERE " + journal_scope, flattened + record_keys
            elif table in {"outbox", "builds"}:
                clause = f" WHERE event_id IN (SELECT event_id FROM journal WHERE {journal_scope})"
                parameters = flattened + record_keys
            elif table == "organizations":
                clause, parameters = (
                    " WHERE id IN (" + ",".join("%s" for _ in pairs) + ")",
                    tuple(pair[1] for pair in pairs),
                )
            else:
                entity_column = "user_id" if table == "memberships" else "id"
                clause, parameters = f" WHERE (tenant_id,{entity_column}) IN ({placeholders})", flattened
        with connection.cursor() as cursor:
            cursor.execute(f"SELECT {columns.get(table, '*')} FROM `{table}`" + clause, parameters)
            for row in cursor:
                if time.monotonic() >= self.deadline:
                    raise TimeoutError("Independent SQL observation deadline exceeded")
                yield row

    def health(self, connection):
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT @@server_uuid AS server_uuid,@@GLOBAL.sync_binlog AS sync_binlog,@@GLOBAL.innodb_flush_log_at_trx_commit AS flush_commit,@@GLOBAL.gtid_mode AS gtid_mode,@@GLOBAL.read_only AS read_only"
            )
            facts = cursor.fetchone()
            if facts is None or facts["sync_binlog"] != 1 or facts["flush_commit"] != 1 or facts["gtid_mode"] != "ON":
                raise StateMismatch("database_durability_incomplete")
            _uuid(facts["server_uuid"])
            cursor.execute(
                "SELECT TABLE_NAME,ENGINE FROM information_schema.tables WHERE TABLE_SCHEMA=%s", (self.target.database,)
            )
            engines = {row["TABLE_NAME"]: row["ENGINE"] for row in cursor}
            if any(
                engines.get(table) != "InnoDB"
                for table in {"journal", "outbox", "entities", "builds", *DOMAIN_TABLES.values()}
            ):
                raise StateMismatch("database_storage_engine_changed")
            cursor.execute("SHOW REPLICA STATUS")
            replicas = list(cursor)
            if (facts["read_only"] and not replicas) or (
                replicas
                and any(
                    row.get("Replica_IO_Running") != "Yes"
                    or row.get("Replica_SQL_Running") != "Yes"
                    or row.get("Last_SQL_Error")
                    or row.get("Last_IO_Error")
                    for row in replicas
                )
            ):
                raise StateMismatch("replica_replay_incomplete")
            return facts


class RegionalDatabaseRecoveryOracle(Oracle):
    FAILURE_CLASSES = {
        "recovery_evidence_unavailable": FailureClass.HARNESS_ERROR,
        "recovery_snapshot_invalid": FailureClass.HARNESS_ERROR,
        "recovery_state_mismatch": FailureClass.AGENT_ERROR,
        "recovery_observation_unavailable": FailureClass.AMBIGUOUS,
        "recovery_container_required": FailureClass.HARNESS_ERROR,
        "recovery_verification_capacity_unavailable": FailureClass.HARNESS_ERROR,
    }

    @property
    def verification_process_limit(self):
        # Each exact HTTP replica has one retained kubectl forward. Account for
        # its Go threads plus one transient SQL/inventory process and Python IO.
        if self.outcomes is None:
            return 256
        replicas = sum(
            target.expected_replicas or 1
            for targets in (
                self.outcomes.api_targets,
                self.outcomes.search_targets,
                self.outcomes.repository_targets,
                self.outcomes.gateway_targets,
            )
            for target in targets
        )
        budget = max(256, 64 + 16 * replicas)
        if budget > 2048:
            raise ValueError("Declared regional observations exceed the private verifier process budget")
        return budget

    def __init__(
        self,
        problem,
        *,
        databases=(),
        cuts=(),
        effect_cuts=(),
        outcomes=None,
        fresh_journal=None,
        stable_seconds=60,
        deadline_seconds=600,
        verification_scratch_bytes=1024**3,
    ):
        super().__init__(problem)
        if type(databases) is not tuple or any(type(item) is not SQLTarget for item in databases):
            raise ValueError("Database inventory must be immutable SQLTarget objects")
        self.databases, self.cuts, self.effect_cuts, self.outcomes = databases, cuts, effect_cuts, outcomes
        # The owner persists this IO resource through its explicit private RPC
        # adapter. Never serialize its live SQLite connection or client thread.
        self.fresh_journal = fresh_journal
        self.baseline_cuts = None
        if (
            type(stable_seconds) is not int
            or type(deadline_seconds) is not int
            or not 0 < stable_seconds < deadline_seconds
        ):
            raise ValueError("Stable observation must fit its bounded deadline")
        self.stable_seconds, self.deadline_seconds = stable_seconds, deadline_seconds
        self.evaluation_timeout_seconds = deadline_seconds + 30
        if type(verification_scratch_bytes) is not int or not 1024**3 <= verification_scratch_bytes <= 8 * 1024**3:
            raise ValueError("Regional verification needs a trusted scratch budget between 1 and 8 GiB")
        self.verification_scratch_bytes = verification_scratch_bytes

    def _protected_state(self, cut, **kwargs):
        # At most three disposable observation spools coexist. Reserve space
        # for repository/artifact observations outside these databases.
        # The named volume is private storage; SQLite enforces each spool cap.
        return ProtectedState(
            cut,
            spool_bytes=(self.verification_scratch_bytes - 128 * 1024**2) // 3,
            scratch_dir="/scratch" if os.environ.get("SREGYM_VERIFIER_CONTAINER") == "1" else None,
            **kwargs,
        )

    def capture_baseline(self):
        if self.baseline_cuts is not None:
            raise RuntimeError("Private recovery baseline is captured once")
        self._validate_snapshot()
        self.baseline_cuts = self.cuts

    def __getstate__(self):
        validate_cut_snapshot_budget(tuple(self.baseline_cuts or ()) + tuple(self.cuts))
        return dict(vars(self))

    def install_verification_snapshot(self, *, cuts, effect_cuts, outcomes):
        if self.baseline_cuts is None:
            raise RuntimeError("Capture the healthy private baseline before preparing observation")
        old_cuts, old_effects, old_outcomes = self.cuts, self.effect_cuts, self.outcomes
        self.cuts, self.effect_cuts, self.outcomes = cuts, effect_cuts, outcomes
        try:
            self._validate_snapshot()
            current = {cut.group: cut for cut in cuts}
            for baseline in self.baseline_cuts:
                after = current[baseline.group]
                if (
                    not set(baseline.closed_epochs) <= set(after.closed_epochs)
                    or not set(baseline.record_keys) <= set(after.record_keys)
                    or after.operations < baseline.operations
                ):
                    raise ValueError("A verification cut cannot discard protected baseline history")
        except Exception:
            self.cuts, self.effect_cuts, self.outcomes = old_cuts, old_effects, old_outcomes
            raise

    def _validate_snapshot(self):
        if (
            type(self.cuts) is not tuple
            or not self.cuts
            or any(type(item) is not ProtectedReceiptCut for item in self.cuts)
        ):
            raise ValueError("Missing private closed receipt cuts")
        if type(self.effect_cuts) is not tuple or any(type(item) is not EffectReceiptCut for item in self.effect_cuts):
            raise ValueError("Missing private effect cuts")
        groups = {cut.group for cut in self.cuts}
        if (
            len(groups) != len(self.cuts)
            or {target.group for target in self.databases} != groups
            or {cut.group for cut in self.effect_cuts} != groups
            or len(self.effect_cuts) != len(groups)
        ):
            raise ValueError("Database, accepted-history and effect groups must match exactly")
        for group in groups:
            members = [target for target in self.databases if target.group == group]
            if (
                len(members) < 4
                or len({target.region for target in members}) < 2
                or len({(target.namespace, target.service) for target in members}) != len(members)
            ):
                raise ValueError("Each group needs distinct real regional database members")
        if type(self.outcomes) is not RecoveryOutcomePlan:
            raise ValueError("Real API, search, Git, build and delivery inventory is required")
        if (
            self.outcomes.require_traffic
            and {challenge.group for challenge in self.outcomes.fresh_challenges} != groups
        ):
            raise ValueError("Continuing work must exercise every frozen database group")

    def _check_sql(self, deadline, *, extra=()):
        cuts = {cut.group: cut for cut in self.cuts}
        effects = {cut.group: cut for cut in self.effect_cuts}
        seen_servers, writers = set(), {group: 0 for group in cuts}
        for target in self.databases:
            reader = SQLObservation(target, deadline=deadline)
            with reader.connection() as connection, self._protected_state(cuts[target.group]) as state:
                facts = reader.health(connection)
                if facts["server_uuid"] in seen_servers:
                    raise StateMismatch("regional_storage_not_independent")
                seen_servers.add(facts["server_uuid"])
                writers[target.group] += facts["read_only"] == 0
                with _observation_phase(
                    "sql-full",
                    operations=state.cut.operations,
                    group=target.group,
                    member=target.namespace + "/" + target.service,
                    observation=getattr(self, "_observation_number", None),
                ):
                    for baseline in self.baseline_cuts:
                        if baseline.group == target.group:
                            with self._protected_state(baseline) as original:
                                original.load_journal(reader.rows(connection, "journal"))
                    state.load_journal(reader.rows(connection, "journal"))
                    state.check_entities(reader.rows(connection, "entities"))
                    for table in DOMAIN_TABLES.values():
                        state.check_domain_rows(table, reader.rows(connection, table))
                    state.verify_domain()
                    state.load_effects(reader.rows(connection, "outbox"), effects[target.group])
                    state.check_build_rows(reader.rows(connection, "builds"))
                yield target, state
                for cut, effect_cut in extra:
                    if cut.group == target.group:
                        with self._protected_state(cut, relationship_state=state) as fresh:

                            def rows(table, reader=reader, connection=connection, keys=cut.record_keys):
                                return reader.rows(connection, table, record_keys=keys)

                            with _observation_phase(
                                "sql-cohort",
                                operations=cut.operations,
                                group=target.group,
                                member=target.namespace + "/" + target.service,
                                observation=getattr(self, "_observation_number", None),
                            ):
                                fresh.load_journal(rows("journal"))
                                fresh.check_entities(rows("entities"))
                                for table in DOMAIN_TABLES.values():
                                    fresh.check_domain_rows(table, rows(table))
                                fresh.verify_domain()
                                fresh.load_effects(rows("outbox"), effect_cut)
                                fresh.check_build_rows(rows("builds"))
                            yield target, fresh
        if any(count != 1 for count in writers.values()):
            raise StateMismatch("regional_writer_fencing_incomplete")

    @staticmethod
    def _body(client, method, url, *, maximum=16 * 1024 * 1024, deadline=None, **kwargs):
        def remaining():
            if deadline is None:
                return 10
            value = deadline - time.monotonic()
            if value <= 0:
                raise TimeoutError("Business observation deadline exceeded")
            return min(10, value)

        if deadline is not None:
            kwargs["timeout"] = remaining()
        kwargs["extensions"] = {"absolute_deadline": time.monotonic() + remaining()}
        content = bytearray()
        with client.stream(method, url, **kwargs) as response:
            if deadline is not None:
                remaining()
            if response.status_code >= 400:
                raise StateMismatch("business_endpoint_unavailable", status=response.status_code)
            # Keep transport chunks visible: coalescing small chunks could let
            # a slow response delay the deadline check indefinitely.
            for block in response.iter_bytes():
                if deadline is not None:
                    remaining()
                if len(content) + len(block) > maximum:
                    raise StateMismatch("business_response_oversized")
                content.extend(block)
        if deadline is not None:
            remaining()
        return bytes(content)

    def _delivery_receipts(self, state, client, *, deadline=float("inf")):
        batch = []
        for effect in state.effect_rows("delivery"):
            if time.monotonic() >= deadline:
                raise TimeoutError("Delivery observation deadline exceeded")
            batch.append(effect["effect_id"])
            if len(batch) == 100:
                yield from self._delivery_batch(batch, client, deadline=deadline)
                batch = []
        if batch:
            yield from self._delivery_batch(batch, client, deadline=deadline)

    def _delivery_batch(self, batch, client, *, deadline=float("inf")):
        if time.monotonic() >= deadline:
            raise TimeoutError("Delivery observation deadline exceeded")
        target = self.outcomes.observer
        if target.transport == "private_pipe":
            response = self._journal_call("delivery_receipts", batch)
        else:
            response = json.loads(
                self._body(
                    client,
                    "GET",
                    target.read_url,
                    params=[("id", value) for value in batch],
                    headers={"Authorization": f"Bearer {target.token}"},
                    deadline=deadline,
                )
            )
        if (
            type(response) is not dict
            or set(response) != {"receipts"}
            or type(response["receipts"]) is not list
            or len(response["receipts"]) > len(batch)
        ):
            if target.transport == "private_pipe":
                raise EvidenceUnavailable("Malformed private observation envelope")
            raise StateMismatch("malformed_independent_receipts")
        if len(canonical(response).encode()) > 16 * 1024 * 1024:
            raise EvidenceUnavailable("Independent observations exceed the bounded receipt frame")
        for receipt in response["receipts"]:
            if type(receipt) is not dict or receipt.get("effect_id") not in batch:
                raise StateMismatch("malformed_independent_receipts")
            yield receipt

    @staticmethod
    def _projection_batches(state, *, search, deadline):
        # The receipt-anchored SQLite cursor stays on the evaluation thread.
        # Worker threads receive only bounded immutable canonical row strings.
        tenant, rows = None, {}
        for op, document in state.latest():
            if time.monotonic() >= deadline:
                raise TimeoutError("Business observation deadline exceeded")
            if search and op.entity_type not in SEARCH_TYPES:
                continue
            if rows and (tenant != op.tenant_id or len(rows) == ENTITY_BATCH_SIZE):
                yield tenant, MappingProxyType(rows)
                rows = {}
            tenant = op.tenant_id
            expected = {
                "tenant_id": tenant,
                "id": op.entity_id,
                "project_id": op.project_id,
                "revision": op.client_revision,
                "document": json.loads(op.payload_json) if search else document,
            }
            expected["kind" if search else "entity_type"] = op.kind if search else op.entity_type
            if op.entity_id in rows:
                raise ValueError("Protected projections contain a duplicate entity identity")
            rows[op.entity_id] = canonical(expected)
        if rows:
            yield tenant, MappingProxyType(rows)

    def _check_projection_batch(self, client, origin, tenant, expected, *, search, token, deadline):
        reason = "search_projection_mismatch" if search else "regional_api_projection_mismatch"
        prefix = "internal" if search else "v1"
        content = self._body(
            client,
            "POST",
            f"{origin}/{prefix}/tenants/{tenant}/entities/lookup",
            json={"ids": list(expected)},
            headers={"Authorization": f"Bearer {token}"},
            maximum=BATCH_RESPONSE_BYTES,
            deadline=deadline,
        )

        def pairs(values):
            result = {}
            for key, value in values:
                if key in result:
                    raise ValueError("Duplicate JSON member")
                result[key] = value
            return result

        def constant(_value):
            raise ValueError("Nonfinite JSON number")

        try:
            response = json.loads(content, object_pairs_hook=pairs, parse_constant=constant)
            if (
                type(response) is not dict
                or set(response) != {"items"}
                or type(response["items"]) is not list
                or len(response["items"]) != len(expected)
            ):
                raise ValueError("Malformed or incomplete entity lookup envelope")
            seen = set()
            fields = {"tenant_id", "id", "project_id", "revision", "document", "kind" if search else "entity_type"}
            for row in response["items"]:
                if time.monotonic() >= deadline:
                    raise TimeoutError("Business observation deadline exceeded")
                if (
                    type(row) is not dict
                    or set(row) != fields
                    or type(row["id"]) is not str
                    or row["id"] not in expected
                    or row["id"] in seen
                    or type(row["tenant_id"]) is not str
                    or row["tenant_id"] != tenant
                    or type(row["revision"]) is not int
                    or row["revision"] < 1
                    or type(row["document"]) is not dict
                    or type(row["kind" if search else "entity_type"]) is not str
                    or len(canonical(row["document"]).encode()) > BATCH_DOCUMENT_BYTES
                    or canonical(row) != expected[row["id"]]
                ):
                    raise ValueError("Entity lookup does not match the protected projection")
                seen.add(row["id"])
            if time.monotonic() >= deadline:
                raise TimeoutError("Business observation deadline exceeded")
        except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
            raise StateMismatch(reason) from exc

    def _check_projections(self, state, client, origins, *, search, deadline):
        if time.monotonic() >= deadline:
            raise TimeoutError("Business observation deadline exceeded")
        if not origins:
            raise ValueError("Every protected projection needs actual replica observations")
        credentials = {value.tenant_id: value.token for value in self.outcomes.fresh_challenges}
        # Only one batch and one bounded wave are in flight. HTTPX clients are
        # shared safely; credentials are request-local and never mutate headers.
        with ThreadPoolExecutor(max_workers=min(BUSINESS_REPLICA_WORKERS, len(origins))) as executor:
            for tenant, expected in self._projection_batches(state, search=search, deadline=deadline):
                token = self.outcomes.service_token if search else credentials.get(tenant)
                if token is None:
                    raise ValueError("Protected tenant has no ordinary customer read credentials")
                for offset in range(0, len(origins), BUSINESS_REPLICA_WORKERS):
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Business observation deadline exceeded")
                    pending = [
                        executor.submit(
                            self._check_projection_batch,
                            client,
                            origin,
                            tenant,
                            expected,
                            search=search,
                            token=token,
                            deadline=deadline,
                        )
                        for origin in origins[offset : offset + BUSINESS_REPLICA_WORKERS]
                    ]
                    try:
                        for future in pending:
                            remaining = deadline - time.monotonic()
                            if remaining <= 0:
                                raise TimeoutError("Business observation deadline exceeded")
                            future.result(timeout=None if remaining == float("inf") else remaining)
                    finally:
                        for future in pending:
                            future.cancel()
        if time.monotonic() >= deadline:
            raise TimeoutError("Business observation deadline exceeded")

    def _check_search(self, state, client, origins, *, deadline=float("inf")):
        self._check_projections(state, client, origins, search=True, deadline=deadline)

    def _git_scratch_guard(self, unlinked_streams):
        if os.environ.get("SREGYM_VERIFIER_CONTAINER") != "1":
            return
        root = Path(os.environ.get("TMPDIR", ""))
        if not root.is_absolute() or root.is_symlink() or not root.is_dir():
            raise VerificationCapacityUnavailable("Private Git scratch ownership is unavailable", resource="storage")
        used = sum(os.fstat(stream.fileno()).st_blocks * 512 for stream in unlinked_streams)
        pending, seen, entries, until = [root], set(), 0, time.monotonic() + 2
        device = root.stat().st_dev
        while pending:
            with os.scandir(pending.pop()) as directory:
                for entry in directory:
                    entries += 1
                    facts = entry.stat(follow_symlinks=False)
                    identity = (facts.st_dev, facts.st_ino)
                    if entries > 20000 or time.monotonic() >= until or facts.st_dev != device:
                        raise VerificationCapacityUnavailable(
                            "Private Git scratch metadata capacity is unavailable", resource="storage"
                        )
                    if identity not in seen:
                        seen.add(identity)
                        used += facts.st_blocks * 512
                        if entry.is_dir(follow_symlinks=False):
                            pending.append(Path(entry.path))
                    if used >= self.verification_scratch_bytes - 1024**2:
                        raise VerificationCapacityUnavailable(
                            "Private Git scratch allocation capacity is unavailable", resource="storage"
                        )

    def _git(self, args, *, token, deadline, maximum=64 * 1024 * 1024, input_bytes=None, environment_extra=None):
        remaining = min(30, deadline - time.monotonic())
        if remaining <= 0:
            raise TimeoutError("Git observation deadline exceeded")
        environment = (
            os.environ
            | {
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "http.extraHeader",
                "GIT_CONFIG_VALUE_0": f"Authorization: Bearer {token}",
            }
            | (environment_extra or {})
        )
        with ExitStack() as files, TemporaryFile() as output, TemporaryFile() as errors, TemporaryFile() as request:
            certificates = {t.ca_certificate for t in self.outcomes.gateway_targets if t.scheme == "https"}
            if certificates:
                from tempfile import NamedTemporaryFile

                authority = files.enter_context(NamedTemporaryFile(mode="w", encoding="utf-8"))
                authority.write("\n".join(sorted(certificates)))
                authority.flush()
                environment["GIT_SSL_CAINFO"] = authority.name
            command = [
                "git",
                "-c",
                "commit.gpgsign=false",
                "-c",
                "credential.helper=",
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "protocol.file.allow=never",
                *args,
            ]
            if input_bytes is not None:
                if len(input_bytes) > maximum:
                    raise StateMismatch("git_transport_or_object_mismatch")
                request.write(input_bytes)
                request.seek(0)
            if os.name == "posix":
                # Each object/log file has a kernel ceiling, even between samples.
                command = [
                    sys.executable,
                    "-c",
                    "import os,resource,sys; resource.setrlimit(resource.RLIMIT_FSIZE,(int(sys.argv[1]),int(sys.argv[1]))); os.execvp(sys.argv[2],sys.argv[2:])",
                    str(max(maximum, 1024 * 1024)),
                    *command,
                ]
            self._git_scratch_guard((output, errors, request))
            result = subprocess.Popen(
                command,
                stdin=request if input_bytes is not None else subprocess.DEVNULL,
                stdout=output,
                stderr=errors,
                env=environment,
                start_new_session=os.name == "posix",
            )
            try:
                until = time.monotonic() + remaining
                while result.poll() is None:
                    if time.monotonic() >= until:
                        raise TimeoutError("Git observation deadline exceeded")
                    if os.fstat(output.fileno()).st_size > maximum or os.fstat(errors.fileno()).st_size > 1024 * 1024:
                        raise StateMismatch("git_transport_or_object_mismatch")
                    self._git_scratch_guard((output, errors, request))
                    time.sleep(min(0.1, max(0, until - time.monotonic())))
                _check_process_capacity()
                self._git_scratch_guard((output, errors, request))
                if result.returncode or output.tell() > maximum or errors.tell() > 1024 * 1024:
                    raise StateMismatch("git_transport_or_object_mismatch")
            finally:
                if os.name == "posix":
                    with suppress(ProcessLookupError):
                        os.killpg(result.pid, signal.SIGKILL)
                elif result.poll() is None:
                    result.kill()
                result.wait(timeout=2)
            output.seek(0)
            return output.read(maximum + 1)

    def _check_git(self, origins, deadline, *, projects=None):
        for project in projects or self.outcomes.projects:
            for origin in origins:
                with TemporaryDirectory(prefix="repository-") as temporary:
                    path = str(Path(temporary) / "repository.git")
                    self._git(
                        ["clone", "--quiet", "--mirror", f"{origin}/git/{project.project_id}", path],
                        token=project.token,
                        deadline=deadline,
                    )
                    self._git(
                        ["-C", path, "fsck", "--full", "--strict", "--no-dangling"],
                        token=project.token,
                        deadline=deadline,
                        maximum=1024 * 1024,
                    )
                    for expected in project.refs:
                        actual = (
                            self._git(
                                ["-C", path, "rev-parse", "--verify", expected.ref],
                                token=project.token,
                                deadline=deadline,
                                maximum=1024,
                            )
                            .decode()
                            .strip()
                        )
                        if actual != expected.commit_sha:
                            raise StateMismatch("git_reference_mismatch")
                    for expected in project.files:
                        content = self._git(
                            ["-C", path, "show", f"{expected.commit_sha}:{expected.path}"],
                            token=project.token,
                            deadline=deadline,
                            maximum=2 * 1024 * 1024,
                        )
                        if hashlib.sha256(content).hexdigest() != expected.sha256:
                            raise StateMismatch("git_content_mismatch")

    def _check_artifacts(self, state, client, origins, *, project_expectations=None, deadline=float("inf")):
        if time.monotonic() >= deadline:
            raise TimeoutError("Artifact observation deadline exceeded")
        projects = {project.project_id: project for project in project_expectations or self.outcomes.projects}
        for build in state.build_rows():
            if time.monotonic() >= deadline:
                raise TimeoutError("Artifact observation deadline exceeded")
            project = projects.get(build["project_id"])
            if project is None:
                raise ValueError("Protected build has no independent repository expectation")
            hashes = {entry.commit_sha: entry.sha256 for entry in project.bundles}
            if build["commit_sha"] not in hashes:
                raise ValueError("Protected build has no independently captured bundle hash")
            if build["artifact_sha256"] != hashes[build["commit_sha"]]:
                raise StateMismatch("build_artifact_identity_mismatch")
            expected_files = {
                entry.path: entry.sha256 for entry in project.files if entry.commit_sha == build["commit_sha"]
            }
            if not expected_files:
                raise ValueError("Protected build has no independent source content hashes")
            for origin in origins:
                content = self._body(
                    client,
                    "GET",
                    f"{origin}/v1/projects/{project.project_id}/artifacts/{build['artifact_sha256']}",
                    maximum=32 * 1024 * 1024,
                    headers={"Authorization": f"Bearer {project.token}"},
                    deadline=deadline,
                )
                if hashlib.sha256(content).hexdigest() != build["artifact_sha256"]:
                    raise StateMismatch("artifact_content_mismatch")
                try:
                    with zipfile.ZipFile(io.BytesIO(content)) as bundle:
                        infos = bundle.infolist()
                        if (
                            len({entry.filename for entry in infos}) != len(infos)
                            or sum(entry.file_size for entry in infos) > 32 * 1024 * 1024
                        ):
                            raise StateMismatch("artifact_content_mismatch")
                        manifest = json.loads(bundle.read("CODEHUB-BUILD.json"))
                        expected = {
                            "project_id": project.project_id,
                            "commit_sha": build["commit_sha"],
                            "builder": "python-validate-bundle-v1",
                            "files": expected_files,
                        }
                        if (
                            canonical(manifest) != canonical(expected)
                            or set(bundle.namelist()) != {*expected_files, "CODEHUB-BUILD.json"}
                            or build["file_count"] != len(expected_files)
                        ):
                            raise StateMismatch("artifact_manifest_mismatch")
                        for name, checksum in expected_files.items():
                            if hashlib.sha256(bundle.read(name)).hexdigest() != checksum:
                                raise StateMismatch("artifact_source_content_mismatch")
                except (zipfile.BadZipFile, KeyError, json.JSONDecodeError) as exc:
                    raise StateMismatch("artifact_content_mismatch") from exc

    def _fresh_api(self, client, origins, repository_origins, deadline, *, issue_only=False):
        """Generate expected values in the verifier, then use ordinary product APIs."""
        epoch = self._journal_call("begin_epoch")
        if type(epoch) is not int or epoch < 0:
            raise EvidenceUnavailable("Fresh receipt owner returned an invalid mutation epoch")
        try:
            return self._fresh_api_epoch(client, origins, repository_origins, deadline, epoch, issue_only=issue_only)
        finally:
            # Benchmark-owned receipts survive failed attempts. Closing a resolved
            # partial epoch preserves its acknowledgments; unresolved IO fails closed.
            if self._journal_call("close_epoch", epoch) is not True:
                raise EvidenceUnavailable("Fresh receipt epoch has unresolved request outcomes")

    def _fresh_api_epoch(self, client, origins, repository_origins, deadline, epoch, *, issue_only=False):
        if not repository_origins or not origins or len(origins) % len(repository_origins):
            raise EvidenceUnavailable("Fresh API and repository entry points cannot be paired by region")
        operations, effects, parent_epochs = {}, {}, {}
        challenges = self.outcomes.fresh_challenges
        if issue_only:
            selected = {}
            for challenge in challenges:
                selected.setdefault(challenge.group, challenge)
            challenges = tuple(selected[group] for group in sorted(selected))
            parents = self._journal_call(
                "project_receipts", [[entry.tenant_id, entry.project_id] for entry in challenges]
            )
            if type(parents) is not list or len(parents) != len(challenges):
                raise EvidenceUnavailable("Private parent provenance is incomplete")
            from sregym.generators.workload.codehub import operation_from_row, validate_effects

            for challenge, parent in zip(challenges, parents, strict=True):
                if (
                    type(parent) is not dict
                    or set(parent) != {"group", "operations", "effects", "epochs"}
                    or parent["group"] != challenge.group
                ):
                    raise EvidenceUnavailable("Private parent provenance differs from the selected group")
                if type(parent["operations"]) is not list or not 1 <= len(parent["operations"]) <= 32:
                    raise EvidenceUnavailable("Private parent history exceeds its bounded observation")
                if (
                    type(parent["effects"]) is not list
                    or not 1 <= len(parent["effects"]) <= 512
                    or any(type(effect) is not dict for effect in parent["effects"])
                    or type(parent["epochs"]) is not list
                    or not 1 <= len(parent["epochs"]) <= 32
                    or any(type(value) is not int or value < 0 for value in parent["epochs"])
                    or parent["epochs"] != sorted(set(parent["epochs"]))
                ):
                    raise EvidenceUnavailable("Malformed bounded private parent provenance")
                parent_events = {row.get("event_id") for row in parent["operations"] if type(row) is dict}
                if {effect.get("event_id") for effect in parent["effects"]} != parent_events:
                    raise EvidenceUnavailable("Private parent effects differ from acknowledged history")
                for row in parent["operations"]:
                    operation = operation_from_row(row)
                    if (
                        operation.tenant_id != challenge.tenant_id
                        or operation.entity_id != challenge.project_id
                        or not operation.kind.startswith("project.")
                    ):
                        raise EvidenceUnavailable("Private parent provenance differs from the customer project")
                    validate_effects(
                        operation,
                        [effect for effect in parent["effects"] if effect.get("event_id") == operation.event_id],
                    )
                operations[challenge.group] = list(parent["operations"])
                effects[challenge.group] = list(parent["effects"])
                parent_epochs[challenge.group] = tuple(sorted(set(parent["epochs"]) | {epoch}))
        for challenge_index, challenge in enumerate(challenges):
            entity = str(uuid4())
            actor = challenge.actor_id
            payloads = (
                {
                    "title": f"Deployment configuration {secrets.token_hex(16)}",
                    "body": secrets.token_urlsafe(32),
                    "state": "open",
                },
                {
                    "title": f"Deployment configuration {secrets.token_hex(16)}",
                    "body": secrets.token_urlsafe(32),
                    "state": "closed",
                },
            )
            submitted = []
            for index, payload in enumerate(payloads):
                body = {
                    "event_id": str(uuid4()),
                    "tenant_id": challenge.tenant_id,
                    "entity_id": entity,
                    "project_id": challenge.project_id,
                    "client_revision": index + 1,
                    "kind": "issue.create" if index == 0 else "issue.update",
                    "payload": payload,
                }
                self._journal_request(epoch, challenge, body)
                response = self._fresh_post(
                    client,
                    f"{origins[(2 * challenge_index + index) % len(origins)]}/v1/operations",
                    body,
                    challenge.token,
                    deadline,
                    journal_epoch=epoch,
                    actor_id=actor,
                )
                self._validate_fresh_ack(body, actor, response)
                submitted.append(body)
                observed = body | {"actor_id": actor}
                operations.setdefault(challenge.group, []).append(observed)
                for kind, destination, payload in [
                    ("search", entity, body),
                    *[
                        ("delivery", hook.id, {"operation": body, "url": hook.url})
                        for hook in challenge.webhooks
                        if body["kind"] in hook.events
                    ],
                ]:
                    effects.setdefault(challenge.group, []).append(
                        {
                            "effect_id": effect_identity(body["event_id"], kind, destination),
                            "event_id": body["event_id"],
                            "effect_kind": kind,
                            "destination": destination,
                            "payload": payload,
                        }
                    )
            # Replaying an earlier acknowledgment cannot create a second effect.
            self._journal_request(epoch, challenge, submitted[0])
            response = self._fresh_post(
                client,
                f"{origins[-1]}/v1/operations",
                submitted[0],
                challenge.token,
                deadline,
                journal_epoch=epoch,
                actor_id=actor,
            )
            self._validate_fresh_ack(submitted[0], actor, response)
            if issue_only:
                continue
            # A Git upload precedes its SQL metadata acknowledgment. The local
            # repository must validate both through the same regional entry.
            # Gateways pair one-to-one; internal APIs are grouped by region.
            repository_index = challenge_index % len(repository_origins)
            receipt_index = repository_index * (len(origins) // len(repository_origins))
            git_request, git_expectation = self._fresh_git(
                challenge, repository_origins[repository_index], deadline, journal_epoch=epoch
            )
            response = self._fresh_post(
                client,
                f"{origins[receipt_index]}/v1/operations",
                git_request,
                challenge.token,
                deadline,
                journal_epoch=epoch,
                actor_id=actor,
            )
            self._validate_fresh_ack(git_request, actor, response)
            operations[challenge.group].append(git_request | {"actor_id": actor})
            effects[challenge.group].extend(self._fresh_effects(git_request, challenge))
            self.fresh_projects = tuple(
                replace(
                    project,
                    refs=project.refs + git_expectation.refs,
                    files=project.files + git_expectation.files,
                    bundles=project.bundles + git_expectation.bundles,
                )
                if project.project_id == challenge.project_id
                else project
                for project in getattr(self, "fresh_projects", self.outcomes.projects)
            )
        return self._operation_cuts(operations, effects, parent_epochs if issue_only else epoch)

    @staticmethod
    def _operation_cuts(operations, effects, epoch):
        cuts = []
        for group, rows in operations.items():
            journal = hashlib.sha256()
            for row in sorted(rows, key=lambda item: item["event_id"]):
                journal.update(canonical(row).encode() + b"\n")
            latest = {}
            for row in rows:
                key = f"{row['tenant_id']}/{row['entity_id']}"
                if key not in latest or latest[key]["client_revision"] < row["client_revision"]:
                    latest[key] = row
            current = hashlib.sha256()
            for key in sorted(latest):
                current.update(canonical(latest[key]).encode() + b"\n")
            checksum = hashlib.sha256()
            for row in sorted(effects[group], key=lambda item: item["effect_id"]):
                checksum.update(canonical(row).encode() + b"\n")
            cuts.append(
                (
                    ProtectedReceiptCut(
                        group,
                        (epoch,) if type(epoch) is int else epoch[group],
                        len(rows),
                        tuple(sorted(latest)),
                        journal.hexdigest(),
                        current.hexdigest(),
                    ),
                    EffectReceiptCut(group, len(effects[group]), checksum.hexdigest()),
                )
            )
        return tuple(cuts)

    def _fresh_post(self, client, url, body, token, deadline, *, journal_epoch=None, actor_id=None):
        for attempt in range(3):
            if time.monotonic() >= deadline:
                break
            try:
                started = time.monotonic()
                content = bytearray()
                remaining = min(10, deadline - time.monotonic())
                if remaining <= 0:
                    raise TimeoutError("Fresh operation observation deadline exceeded")
                with client.stream(
                    "POST",
                    url,
                    json=body,
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=remaining,
                    extensions={"absolute_deadline": min(deadline, started + 10)},
                ) as response:
                    status = response.status_code
                    for block in response.iter_bytes():
                        if time.monotonic() >= deadline:
                            raise TimeoutError("Fresh operation observation deadline exceeded")
                        content.extend(block)
                        if len(content) > 256 * 1024:
                            raise StateMismatch("fresh_acknowledgment_oversized")
                if time.monotonic() >= deadline:
                    raise TimeoutError("Fresh operation observation deadline exceeded")
                latency_ms = (time.monotonic() - started) * 1000
                if status not in {200, 201}:
                    if journal_epoch is not None and status in {400, 401, 403, 404, 409, 422}:
                        if (
                            self._journal_call("reject", journal_epoch, body["event_id"], url, latency_ms, status)
                            is not True
                        ):
                            raise EvidenceUnavailable("Fresh request rejection was not durably recorded")
                    raise StateMismatch("business_endpoint_unavailable", status=status)
                try:
                    acknowledgment = json.loads(content)
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise StateMismatch("fresh_commit_acknowledgment_mismatch") from exc
                if journal_epoch is not None:
                    self._validate_fresh_ack(body, actor_id, acknowledgment)
                    if (
                        self._journal_call(
                            "acknowledge", journal_epoch, body["event_id"], url, latency_ms, status, acknowledgment
                        )
                        is not True
                    ):
                        raise EvidenceUnavailable("Fresh acknowledgment was not durably recorded")
                return acknowledgment
            except StateMismatch as exc:
                if exc.reason != "business_endpoint_unavailable" or exc.detail.get("status") not in {
                    429,
                    500,
                    502,
                    503,
                    504,
                }:
                    raise
            except httpx.TransportError:
                pass
            if attempt < 2:
                time.sleep(min(0.25, max(0, deadline - time.monotonic())))
        raise TimeoutError("Fresh operation outcome unresolved after bounded identical-identity retries")

    def _journal_request(self, epoch, challenge, body, provenance=None):
        if (
            self._journal_call(
                "request",
                epoch,
                challenge.group,
                body | {"actor_id": challenge.actor_id},
                self._fresh_effects(body, challenge),
                provenance,
            )
            is not True
        ):
            raise EvidenceUnavailable("Fresh request was not durably recorded before application IO")

    def _journal_call(self, method, *args):
        try:
            return getattr(self.fresh_journal, method)(*args)
        except (AttributeError, RuntimeError, ValueError, TypeError, OSError, TimeoutError) as exc:
            raise EvidenceUnavailable("The private fresh receipt owner is unavailable") from exc

    @staticmethod
    def _fresh_effects(body, challenge):
        entries = [("search", body["entity_id"], body)]
        if body["kind"] == "repository.push":
            entries.append(("build", body["project_id"], body | {"commit_sha": body["payload"]["commit_sha"]}))
        entries.extend(
            ("delivery", hook.id, {"operation": body, "url": hook.url})
            for hook in challenge.webhooks
            if body["kind"] in hook.events
        )
        return [
            {
                "effect_id": effect_identity(body["event_id"], kind, destination),
                "event_id": body["event_id"],
                "effect_kind": kind,
                "destination": destination,
                "payload": payload,
            }
            for kind, destination, payload in entries
        ]

    def _fresh_git(self, challenge, origin, deadline, *, journal_epoch=None):
        """A normal authenticated push with independently known new source bytes."""
        project = next((value for value in self.outcomes.projects if value.project_id == challenge.project_id), None)
        if project is None:
            raise ValueError("Fresh Git challenge has no private source expectations")
        parent = project.refs[0].commit_sha
        original = {entry.path: entry.sha256 for entry in project.files if entry.commit_sha == parent}
        if not original:
            raise ValueError("Fresh Git parent has no independent source hashes")
        with TemporaryDirectory(prefix="customer-change-") as temporary:
            path = str(Path(temporary) / "repository.git")
            self._git(
                ["clone", "--quiet", "--mirror", f"{origin}/git/{project.project_id}", path],
                token=challenge.token,
                deadline=deadline,
            )
            files = {}
            for name, expected in original.items():
                content = self._git(
                    ["-C", path, "show", f"{parent}:{name}"],
                    token=challenge.token,
                    deadline=deadline,
                    maximum=2 * 1024 * 1024,
                )
                if hashlib.sha256(content).hexdigest() != expected:
                    raise StateMismatch("fresh_git_parent_content_mismatch")
                files[name] = content
            self._git(["-C", path, "read-tree", parent], token=challenge.token, deadline=deadline)
            new_path = f"deploy/config_{uuid4().hex}.json"
            files[new_path] = canonical({"enabled": True, "release": secrets.token_urlsafe(32)}).encode()
            blob = (
                self._git(
                    ["-C", path, "hash-object", "-w", "--stdin"],
                    token=challenge.token,
                    deadline=deadline,
                    input_bytes=files[new_path],
                    maximum=1024,
                )
                .decode()
                .strip()
            )
            self._git(
                ["-C", path, "update-index", "--add", "--cacheinfo", "100644", blob, new_path],
                token=challenge.token,
                deadline=deadline,
            )
            tree = (
                self._git(["-C", path, "write-tree"], token=challenge.token, deadline=deadline, maximum=1024)
                .decode()
                .strip()
            )
            author = {
                "GIT_AUTHOR_NAME": "Release automation",
                "GIT_AUTHOR_EMAIL": "automation@example.test",
                "GIT_COMMITTER_NAME": "Release automation",
                "GIT_COMMITTER_EMAIL": "automation@example.test",
            }
            commit = (
                self._git(
                    ["-C", path, "commit-tree", tree, "-p", parent],
                    token=challenge.token,
                    deadline=deadline,
                    input_bytes=b"Update deployment configuration\n",
                    maximum=1024,
                    environment_extra=author,
                )
                .decode()
                .strip()
            )
            _sha(commit, 40)
            ref = f"refs/heads/change-{uuid4().hex}"
            request, expectation = self._materialize_git_probe(challenge, project, files, commit, ref)
            if journal_epoch is not None:
                provenance = {
                    "project_id": expectation.project_id,
                    "refs": [{"ref": value.ref, "commit_sha": value.commit_sha} for value in expectation.refs],
                    "files": [
                        {"commit_sha": value.commit_sha, "path": value.path, "sha256": value.sha256}
                        for value in expectation.files
                    ],
                    "bundles": [
                        {"commit_sha": value.commit_sha, "sha256": value.sha256} for value in expectation.bundles
                    ],
                }
                self._journal_request(journal_epoch, challenge, request, provenance)
            self._git(
                ["-C", path, "-c", "remote.origin.mirror=false", "push", "--quiet", "origin", f"{commit}:{ref}"],
                token=challenge.token,
                deadline=deadline,
            )
        return request, expectation

    @staticmethod
    def _materialize_git_probe(challenge, project, files, commit, ref):
        manifest = {
            "project_id": project.project_id,
            "commit_sha": commit,
            "builder": "python-validate-bundle-v1",
            "files": {name: hashlib.sha256(content).hexdigest() for name, content in sorted(files.items())},
        }
        if sum(len(content) for content in files.values()) > 32 * 1024 * 1024:
            raise ValueError("Fresh source fixture exceeds the actual builder's capacity")
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
            for name, content in [*sorted(files.items()), ("CODEHUB-BUILD.json", canonical(manifest).encode())]:
                info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                info.external_attr = 0o644 << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                bundle.writestr(info, content)
        expectation = GitProjectExpectation(
            project.project_id,
            project.token,
            (GitRefExpectation(ref, commit),),
            tuple(GitFileExpectation(commit, name, checksum) for name, checksum in manifest["files"].items()),
            (GitBundleExpectation(commit, hashlib.sha256(output.getvalue()).hexdigest()),),
        )
        request = {
            "event_id": str(uuid4()),
            "tenant_id": challenge.tenant_id,
            "entity_id": str(uuid4()),
            "project_id": project.project_id,
            "client_revision": 1,
            "kind": "repository.push",
            "payload": {"ref": ref, "commit_sha": commit},
        }
        return request, expectation

    def _check_api_current(self, state, client, origins, *, deadline=float("inf")):
        self._check_projections(state, client, origins, search=False, deadline=deadline)

    @staticmethod
    def _validate_fresh_ack(body, actor, response):
        if (
            type(response) is not dict
            or any(response.get(name) != value for name, value in (body | {"actor_id": actor}).items())
            or response.get("record_key") != f"{body['tenant_id']}/{body['entity_id']}"
            or response.get("operation_sha256") != digest(body | {"actor_id": actor})
        ):
            raise StateMismatch("fresh_commit_acknowledgment_mismatch")

    def _http_inventory(self, deadline):
        result = {}
        for role, targets in (
            ("api", self.outcomes.api_targets),
            ("search", self.outcomes.search_targets),
            ("repository", self.outcomes.repository_targets),
            ("gateway", self.outcomes.gateway_targets),
        ):
            if role == "gateway" and not targets:
                continue
            observed = tuple(value for target in targets for value in resolve_http_replicas(target, deadline=deadline))
            if not observed or len({uid for _target, uid in observed}) != len(observed):
                raise StateMismatch("regional_replica_inventory_mismatch")
            result[role] = observed
        return result

    def _verify_recent_business(self, cuts, client, origins, deadline):
        seen_servers, writers = set(), {target.group: 0 for target in self.databases}
        if {cut.group for cut, _effects in cuts} != set(writers) or len(cuts) != len(writers):
            raise EvidenceUnavailable("Final customer proof omits or repeats a database group")
        for target in self.databases:
            reader = SQLObservation(target, deadline=deadline)
            with reader.connection() as connection:
                facts = reader.health(connection)
                if facts["server_uuid"] in seen_servers:
                    raise StateMismatch("regional_storage_not_independent")
                seen_servers.add(facts["server_uuid"])
                writers[target.group] += facts["read_only"] == 0
                for cut, effects in cuts:
                    if cut.group != target.group:
                        continue
                    with self._protected_state(cut) as state:

                        def rows(table, reader=reader, connection=connection, keys=cut.record_keys):
                            return reader.rows(connection, table, record_keys=keys)

                        state.load_journal(rows("journal"))
                        state.check_entities(rows("entities"))
                        for table in DOMAIN_TABLES.values():
                            state.check_domain_rows(table, rows(table))
                        state.verify_domain()
                        state.load_effects(rows("outbox"), effects)
                        state.check_build_rows(rows("builds"))
                        self._check_api_current(
                            state, client, origins["api"] + origins.get("gateway", ()), deadline=deadline
                        )
                        self._check_search(state, client, origins["search"], deadline=deadline)
                        state.check_delivery_receipts(self._delivery_receipts(state, client, deadline=deadline))
        if any(count != 1 for count in writers.values()):
            raise StateMismatch("regional_writer_fencing_incomplete")

    def _final_live_probe(self, client, origins, deadline):
        self._final_probe_open = True
        cuts = self._fresh_api(
            client,
            origins.get("gateway", origins["api"]),
            origins.get("gateway", origins["repository"]),
            deadline,
            issue_only=True,
        )
        retained = getattr(self, "_retained_final_cuts", ())
        if len(retained) >= 16:
            raise EvidenceUnavailable("Final observation cohort budget exceeded")
        self._retained_final_cuts = retained + (cuts,)
        self._final_probe_open = False
        settle_deadline = min(deadline, time.monotonic() + 90)
        while True:
            try:
                for accepted in self._retained_final_cuts:
                    self._verify_recent_business(accepted, client, origins, settle_deadline)
                return
            except StateMismatch:
                if time.monotonic() >= settle_deadline:
                    raise
                time.sleep(min(1, max(0, settle_deadline - time.monotonic())))

    def _process_inventory(self, deadline):
        observed = []
        for target in self.outcomes.process_targets:
            replicas = resolve_http_replicas(
                HTTPServiceTarget(
                    target.region,
                    target.namespace,
                    target.name,
                    resource_kind=target.kind,
                    expected_replicas=target.replicas,
                    expected_uid=target.uid,
                ),
                deadline=deadline,
            )
            if target.name == "worker":
                # Redundant workers may legitimately recycle during a long
                # business scan. Still validate every ready owned replica and
                # the captured controller, then grade their actual outcomes.
                observed.append((target.namespace, target.name, target.uid))
            else:
                observed.extend((target.namespace, target.name, uid) for _pod, uid in replicas)
        return tuple(observed)

    def _traffic_observations(self):
        response = self._journal_call("traffic_progress")
        if (
            type(response) is not dict
            or set(response) != {"running", "groups"}
            or type(response["running"]) is not bool
        ):
            raise EvidenceUnavailable("Malformed private continuing traffic facts")
        groups = {cut.group for cut in self.cuts}
        entries = response["groups"]
        if type(entries) is not list or len(entries) > len(groups):
            raise EvidenceUnavailable("Private traffic observation exceeded its bounded group inventory")
        if not response["running"] or {entry.get("group") for entry in entries if type(entry) is dict} != groups:
            raise StateMismatch("continuing_customer_traffic_unavailable")
        positions, cuts = {}, []
        for entry in entries:
            if type(entry) is not dict or set(entry) != {
                "group",
                "sequence",
                "epoch",
                "age_seconds",
                "operations",
                "effects",
            }:
                raise EvidenceUnavailable("Malformed private customer journey")
            age, sequence, epoch = entry["age_seconds"], entry["sequence"], entry["epoch"]
            if (
                type(sequence) is not int
                or sequence <= 0
                or type(epoch) is not int
                or epoch < 0
                or type(age) not in {int, float}
                or not math.isfinite(age)
                or age < 0
            ):
                raise EvidenceUnavailable("Malformed private customer progress counters")
            if age > 30:
                raise StateMismatch("continuing_customer_traffic_stalled")
            rows, effects = entry["operations"], entry["effects"]
            if type(rows) is not list or len(rows) != 2 or type(effects) is not list or not 2 <= len(effects) <= 16:
                raise EvidenceUnavailable("Malformed bounded customer journey expectations")
            from sregym.generators.workload.codehub import operation_from_row, validate_effects

            operations = tuple(operation_from_row(row) for row in rows)
            first, final = operations
            if (
                first.kind != "issue.create"
                or final.kind != "issue.update"
                or first.client_revision != 1
                or final.client_revision != 2
                or first.record_key != final.record_key
                or first.project_id != final.project_id
                or first.actor_id != final.actor_id
            ):
                raise EvidenceUnavailable("Owner traffic journey lacks complete create/update provenance")
            for operation in operations:
                validate_effects(
                    operation, [effect for effect in effects if effect.get("event_id") == operation.event_id]
                )
            group = entry["group"]
            if group in positions:
                raise EvidenceUnavailable("Private traffic observation repeated a database group")
            positions[group] = sequence
            cuts.extend(self._operation_cuts({group: rows}, {group: effects}, epoch))
        return positions, tuple(cuts)

    def evaluate(self, solution=None, trace=None, duration=None):
        if os.environ.get("SREGYM_VERIFIER_CONTAINER") != "1":
            return self.fail("recovery_container_required")
        if self.baseline_cuts is None:
            return self.fail("recovery_evidence_unavailable", check="baseline")
        try:
            self._validate_snapshot()
        except ValueError:
            return self.fail("recovery_snapshot_invalid")
        if self.fresh_journal is None:
            return self.fail("recovery_evidence_unavailable", check="persistent_fresh_receipts")
        deadline, stable_since = time.monotonic() + self.deadline_seconds, None
        fresh, last_mismatch, last_transport_error = None, None, False
        traffic_start = None
        reported_mismatch = None
        self._retained_final_cuts, self._final_probe_open = (), False
        self._observation_number = 0
        try:
            import ssl

            from sregym.generators.workload.http_deadline import DeadlineTransport

            tls = ssl.create_default_context()
            for target in self.outcomes.gateway_targets:
                if target.scheme == "https":
                    tls.load_verify_locations(cadata=target.ca_certificate)
            with (
                ExitStack() as stack,
                httpx.Client(
                    timeout=10,
                    headers={"Authorization": f"Bearer {self.outcomes.service_token}"},
                    follow_redirects=False,
                    verify=tls,
                    transport=DeadlineTransport(verify=tls),
                ) as client,
            ):
                forwarding = stack.enter_context(ExitStack())
                origins, inventory_signature = {}, None
                while time.monotonic() < deadline:
                    self._observation_number += 1
                    try:
                        processes = self._process_inventory(deadline)
                        inventory = self._http_inventory(deadline)
                        signature = tuple(
                            (role, target.namespace, target.service, target.port, target.resource_kind, uid)
                            for role, replicas in inventory.items()
                            for target, uid in replicas
                        )
                        signature += processes
                        if signature != inventory_signature:
                            forwarding.close()
                            origins = {
                                role: tuple(
                                    f"{target.scheme}://127.0.0.1:{forwarding.enter_context(port_forward(target.namespace, target.service, target.port, deadline=deadline, resource_kind=target.resource_kind))}"
                                    for target, _uid in replicas
                                )
                                for role, replicas in inventory.items()
                            }
                            inventory_signature, stable_since, traffic_start = signature, None, None
                        continuing, traffic_positions = (), None
                        if fresh is not None and self.outcomes.require_traffic:
                            traffic_positions, continuing = self._traffic_observations()
                        builds, deliveries, observed_cuts = 0, 0, set()
                        retained = tuple(cut for cohort in self._retained_final_cuts for cut in cohort)
                        for _target, state in self._check_sql(deadline, extra=(fresh or ()) + continuing + retained):
                            cut = state.cut
                            identity = (
                                cut.group,
                                cut.closed_epochs,
                                cut.operations,
                                cut.journal_sha256,
                                cut.current_sha256,
                            )
                            if identity not in observed_cuts:
                                with _observation_phase(
                                    "api",
                                    operations=cut.operations,
                                    group=cut.group,
                                    observation=self._observation_number,
                                    replicas=len(origins["api"] + origins.get("gateway", ())),
                                ):
                                    self._check_api_current(
                                        state, client, origins["api"] + origins.get("gateway", ()), deadline=deadline
                                    )
                                with _observation_phase(
                                    "search",
                                    operations=cut.operations,
                                    group=cut.group,
                                    observation=self._observation_number,
                                    replicas=len(origins["search"]),
                                ):
                                    self._check_search(state, client, origins["search"], deadline=deadline)
                                with _observation_phase(
                                    "delivery",
                                    operations=cut.operations,
                                    group=cut.group,
                                    observation=self._observation_number,
                                ):
                                    state.check_delivery_receipts(
                                        self._delivery_receipts(state, client, deadline=deadline)
                                    )
                                with _observation_phase(
                                    "artifacts",
                                    operations=cut.operations,
                                    group=cut.group,
                                    observation=self._observation_number,
                                    replicas=len(origins["api"]),
                                ):
                                    self._check_artifacts(
                                        state,
                                        client,
                                        origins["api"],
                                        project_expectations=getattr(self, "fresh_projects", None),
                                        deadline=deadline,
                                    )
                                observed_cuts.add(identity)
                            builds += state.db.execute("SELECT COUNT(*) FROM builds").fetchone()[0]
                            deliveries += state.db.execute(
                                "SELECT COUNT(*) FROM effects WHERE kind='delivery'"
                            ).fetchone()[0]
                        if not builds or not deliveries:
                            return self.fail("recovery_evidence_unavailable", check="complete_business_baseline")
                        with _observation_phase(
                            "git", observation=self._observation_number, replicas=len(origins["repository"])
                        ):
                            self._check_git(
                                origins["repository"], deadline, projects=getattr(self, "fresh_projects", None)
                            )
                        last_mismatch, last_transport_error = None, False
                        if fresh is None:
                            try:
                                fresh = self._fresh_api(
                                    client,
                                    origins.get("gateway", origins["api"]),
                                    origins.get("gateway", origins["repository"]),
                                    deadline,
                                )
                            except StateMismatch as exc:
                                return self.fail("recovery_state_mismatch", check=exc.reason, **exc.detail)
                            except (httpx.HTTPError, TimeoutError):
                                return self.fail(
                                    "recovery_observation_unavailable", check="fresh_request_outcome_unresolved"
                                )
                            continue
                        if stable_since is None:
                            stable_since = time.monotonic()
                            traffic_start = traffic_positions
                        if self.outcomes.require_traffic:
                            self._traffic_observations()  # Recheck actual owner progress after long business reads.
                        final_inventory = self._http_inventory(deadline)
                        final_signature = tuple(
                            (role, target.namespace, target.service, target.port, target.resource_kind, uid)
                            for role, replicas in final_inventory.items()
                            for target, uid in replicas
                        ) + self._process_inventory(deadline)
                        if final_signature != inventory_signature:
                            stable_since, traffic_start = None, None
                            continue
                        observed_at = time.monotonic()
                        if observed_at >= deadline:
                            raise TimeoutError("Business observation deadline exceeded")
                        if self.outcomes.require_traffic and not all(
                            traffic_positions[group] > traffic_start[group] for group in traffic_start
                        ):
                            last_mismatch = StateMismatch("continuing_customer_traffic_stalled")
                        if observed_at - stable_since >= self.stable_seconds:
                            if self.outcomes.require_traffic and not all(
                                traffic_positions[group] > traffic_start[group] for group in traffic_start
                            ):
                                raise StateMismatch("continuing_customer_traffic_stalled")
                            if self.outcomes.require_traffic:
                                self._final_live_probe(client, origins, deadline)
                                self._traffic_observations()
                                final_inventory = self._http_inventory(deadline)
                                final_signature = tuple(
                                    (role, target.namespace, target.service, target.port, target.resource_kind, uid)
                                    for role, replicas in final_inventory.items()
                                    for target, uid in replicas
                                ) + self._process_inventory(deadline)
                                if final_signature != inventory_signature:
                                    stable_since, traffic_start = None, None
                                    continue
                            return {"success": True}
                    except StateMismatch as exc:
                        _check_process_capacity()
                        if reported_mismatch != exc.reason:
                            logging.warning("Private recovery observation mismatch: %s", exc.reason)
                            reported_mismatch = exc.reason
                        if self._final_probe_open:
                            return self.fail("recovery_state_mismatch", check=exc.reason, **exc.detail)
                        if exc.reason == "verification_spool_capacity_exceeded":
                            return self.fail("recovery_verification_capacity_unavailable")
                        last_mismatch, last_transport_error, stable_since, traffic_start = exc, False, None, None
                    except (httpx.HTTPError, OSError, TimeoutError):
                        _check_process_capacity()
                        if self._final_probe_open:
                            return self.fail(
                                "recovery_observation_unavailable", check="final_request_outcome_unresolved"
                            )
                        last_mismatch, last_transport_error, stable_since, traffic_start = None, True, None, None
                    time.sleep(min(2, max(0, deadline - time.monotonic())))
                if last_mismatch is not None:
                    return self.fail("recovery_state_mismatch", check=last_mismatch.reason, **last_mismatch.detail)
                return self.fail("recovery_observation_unavailable", transport_unavailable=last_transport_error)
        except VerificationCapacityUnavailable as error:
            return self.fail("recovery_verification_capacity_unavailable", resource=error.resource)
        except EvidenceUnavailable:
            return self.fail("recovery_evidence_unavailable", check="persistent_fresh_receipts")
        except ValueError:
            return self.fail("recovery_evidence_unavailable", check="independent_business_expectations")
        except (OSError, RuntimeError, TimeoutError, subprocess.SubprocessError, httpx.HTTPError):
            try:
                _check_process_capacity()
            except VerificationCapacityUnavailable:
                return self.fail("recovery_verification_capacity_unavailable", resource="processes")
            logging.exception("Private recovery observation failed")
            return self.fail("recovery_observation_unavailable")
