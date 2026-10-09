"""Runner-owned client receipts for ordinary authenticated application traffic.

Requests, acknowledgments and unresolved outcomes are distinct. Receipt storage
stays outside workload volumes; no server-provided recovery verdict is trusted.
"""

import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit
from uuid import UUID

import httpx

from sregym.generators.workload.http_deadline import DeadlineTransport


def canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _identity(value: str) -> None:
    if type(value) is not str or str(UUID(value)) != value:
        raise ValueError("Identity must be a canonical UUID")


@dataclass(frozen=True)
class Operation:
    event_id: str
    tenant_id: str
    entity_id: str
    project_id: str | None
    client_revision: int
    kind: str
    payload_json: str
    actor_id: str

    def __post_init__(self):
        for value in (self.event_id, self.tenant_id, self.entity_id, self.actor_id):
            _identity(value)
        if self.project_id is not None:
            _identity(self.project_id)
        if type(self.client_revision) is not int or self.client_revision < 1:
            raise ValueError("Client revision must be a positive integer")
        if type(self.kind) is not str or not self.kind or len(self.kind) > 64:
            raise ValueError("Operation kind must be a bounded string")
        payload = json.loads(self.payload_json)
        if type(payload) is not dict or canonical(payload) != self.payload_json:
            raise ValueError("Payload must be a canonical JSON object")

    def request(self) -> dict:
        return {
            "event_id": self.event_id,
            "tenant_id": self.tenant_id,
            "entity_id": self.entity_id,
            "project_id": self.project_id,
            "client_revision": self.client_revision,
            "kind": self.kind,
            "payload": json.loads(self.payload_json),
        }

    @property
    def record_key(self) -> str:
        return f"{self.tenant_id}/{self.entity_id}"

    def observed_row(self) -> dict:
        return self.request() | {"actor_id": self.actor_id}


@dataclass(frozen=True)
class ReceiptCut:
    closed_epochs: tuple[int, ...]
    operations: int
    entities: tuple[str, ...]
    journal_sha256: str
    current_sha256: str


def operation_from_row(row: dict) -> Operation:
    fields = {"event_id", "tenant_id", "entity_id", "project_id", "client_revision", "kind", "payload", "actor_id"}
    if type(row) is not dict or set(row) != fields:
        raise ValueError("Expected a complete ordinary operation with its actor")
    if len(canonical(row).encode()) > 132 * 1024:
        raise ValueError("Operation exceeds the bounded receipt size")
    return Operation(
        **{key: value for key, value in row.items() if key != "payload"}, payload_json=canonical(row["payload"])
    )


def _digest(value, length=64):
    if type(value) is not str or re.fullmatch(f"[0-9a-f]{{{length}}}", value) is None:
        raise ValueError("Invalid immutable content digest")


def validate_effects(operation: Operation, effects) -> list[dict]:
    if type(effects) not in (list, tuple) or len(effects) > 100:
        raise ValueError("Expected a bounded list of independent effects")
    result, identities = [], set()
    for effect in effects:
        if type(effect) is not dict or set(effect) != {
            "effect_id",
            "event_id",
            "effect_kind",
            "destination",
            "payload",
        }:
            raise ValueError("Invalid expected effect fields")
        kind, destination = effect["effect_kind"], effect["destination"]
        if kind not in {"search", "build", "delivery"}:
            raise ValueError("Invalid effect kind")
        _identity(destination)
        identity = hashlib.sha256(f"{operation.event_id}/{kind}/{destination}".encode()).hexdigest()
        if effect["event_id"] != operation.event_id or effect["effect_id"] != identity or identity in identities:
            raise ValueError("Effect identity does not match its requested operation")
        payload = effect["payload"]
        expected = operation.request()
        if kind == "search":
            if destination != operation.entity_id or payload != expected:
                raise ValueError("Search expectation differs from the requested operation")
        elif kind == "build":
            commit = expected["payload"].get("commit_sha") or expected["payload"].get("head_sha")
            if destination != operation.project_id or not commit or payload != expected | {"commit_sha": commit}:
                raise ValueError("Build expectation differs from the requested commit")
        else:
            if type(payload) is not dict or set(payload) != {"operation", "url"} or payload["operation"] != expected:
                raise ValueError("Delivery expectation differs from the requested operation")
            parsed = urlsplit(payload["url"])
            if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
                raise ValueError("Invalid ordinary delivery destination")
        identities.add(identity)
        result.append(json.loads(canonical(effect)))
    return sorted(result, key=lambda row: row["effect_id"])


def validate_git_provenance(operation: Operation, provenance: dict) -> dict:
    if operation.kind != "repository.push" or type(provenance) is not dict:
        raise ValueError("Git provenance requires a repository push")
    if (
        set(provenance) != {"project_id", "refs", "files", "bundles"}
        or provenance["project_id"] != operation.project_id
    ):
        raise ValueError("Git provenance must belong to the requested project")
    if len(canonical(provenance).encode()) > 128 * 1024:
        raise ValueError("Git provenance exceeds its bounded size")
    commit, ref = operation.request()["payload"].get("commit_sha"), operation.request()["payload"].get("ref")
    _digest(commit, 40)
    for key in ("refs", "files", "bundles"):
        if type(provenance[key]) is not list or not provenance[key] or len(provenance[key]) > 500:
            raise ValueError("Git provenance needs bounded nonempty content inventories")
    if provenance["refs"] != [{"ref": ref, "commit_sha": commit}]:
        raise ValueError("Git reference provenance differs from the requested push")
    if (
        type(ref) is not str
        or not ref.startswith("refs/heads/")
        or any(token in ref for token in ("..", "@{", "\\", " ", "~", "^", ":", "?", "*", "["))
        or ref.endswith(("/", ".", ".lock"))
        or "//" in ref
    ):
        raise ValueError("Invalid Git reference")
    paths = set()
    for row in provenance["files"]:
        if type(row) is not dict or set(row) != {"commit_sha", "path", "sha256"} or row["commit_sha"] != commit:
            raise ValueError("Invalid Git file provenance")
        path = row["path"]
        if type(path) is not str or not path or PurePosixPath(path).is_absolute() or ".." in PurePosixPath(path).parts:
            raise ValueError("Git file paths must stay within the repository")
        if "\\" in path or "\x00" in path or path in paths:
            raise ValueError("Invalid or duplicate Git file path")
        _digest(row["sha256"])
        paths.add(path)
    if len(provenance["bundles"]) != 1:
        raise ValueError("One immutable bundle is expected per fresh commit")
    bundle = provenance["bundles"][0]
    if type(bundle) is not dict or set(bundle) != {"commit_sha", "sha256"} or bundle["commit_sha"] != commit:
        raise ValueError("Invalid Git bundle provenance")
    _digest(bundle["sha256"])
    return json.loads(canonical(provenance))


class ReceiptLedger:
    """Durable private provenance, with closure rejected while outcomes are unknown."""

    def __init__(self, path: Path, *, byte_budget=32 * 1024**3):
        if type(byte_budget) is not int or byte_budget < 4 * 1024**2:
            raise ValueError("Private receipt storage requires at least a bounded 4 MiB budget")
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._path = path.resolve()
        self._read_only = False
        self._byte_budget = byte_budget
        self.capacity_error = None
        os.chmod(path, 0o600)
        self._db.executescript(
            """
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=FULL;
            CREATE TABLE IF NOT EXISTS epochs(id INTEGER PRIMARY KEY, closed INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS requests(
                event_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, record_key TEXT NOT NULL, revision INTEGER NOT NULL,
                epoch INTEGER NOT NULL REFERENCES epochs(id), body TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('pending','acknowledged','rejected')),
                origin TEXT, latency_ms REAL, status_code INTEGER,
                UNIQUE(record_key,revision)
            );
            CREATE INDEX IF NOT EXISTS receipt_epoch ON requests(epoch,status,event_id);
            CREATE INDEX IF NOT EXISTS receipt_status_epoch ON requests(status,epoch,event_id);
            CREATE INDEX IF NOT EXISTS receipt_tenant ON requests(tenant_id);
            CREATE TABLE IF NOT EXISTS expectations(
                event_id TEXT PRIMARY KEY REFERENCES requests(event_id),
                effects TEXT NOT NULL, provenance TEXT
            );
            """
        )
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.execute("PRAGMA wal_autocheckpoint=64")
        page_size = self._db.execute("PRAGMA page_size").fetchone()[0]
        self._db.execute(f"PRAGMA max_page_count={byte_budget // (2 * page_size)}")

    @contextmanager
    def read_snapshot(self, *, deadline_seconds=600, cancel=None, maximum_wal_growth_bytes=256 * 1024**2):
        """Pin one WAL read transaction without holding the live writer lock."""
        if not 0 < deadline_seconds <= 600 or not 0 < maximum_wal_growth_bytes <= 1024**3:
            raise ValueError("Private snapshot preparation needs bounded time and WAL growth")
        deadline = time.monotonic() + deadline_seconds
        wal = self._path.with_name(self._path.name + "-wal")
        initial_wal = wal.stat().st_size if wal.exists() else 0
        next_storage_check, interrupted = 0.0, None

        def progress():
            nonlocal next_storage_check, interrupted
            now = time.monotonic()
            if now >= deadline or (cancel is not None and cancel.is_set()):
                interrupted = "Private snapshot preparation deadline or cancellation"
                return 1
            if now >= next_storage_check:
                next_storage_check = now + 1
                growth = (wal.stat().st_size if wal.exists() else 0) - initial_wal
                if growth > maximum_wal_growth_bytes or shutil.disk_usage(self._path.parent).free < 256 * 1024**2:
                    interrupted = "Private snapshot preparation storage capacity unavailable"
                    return 1
            return 0

        view = object.__new__(ReceiptLedger)
        view._lock = threading.RLock()
        view._path = self._path
        view._read_only = True
        view._byte_budget = self._byte_budget
        view._snapshot_guard = progress
        view._db = sqlite3.connect(self._path.as_uri() + "?mode=ro", uri=True, check_same_thread=False)
        view._db.set_progress_handler(progress, 1000)
        try:
            view._db.execute("PRAGMA query_only=ON")
            with self._lock:
                view._db.execute("BEGIN")
                # Establish the entire database's snapshot before releasing the
                # owner lock. Subsequent tables and closed epochs share this cut.
                view._db.execute("SELECT id FROM epochs LIMIT 1").fetchone()
            yield view
            if progress():
                raise TimeoutError(interrupted)
        except sqlite3.OperationalError as error:
            if interrupted is not None:
                raise TimeoutError(interrupted) from error
            raise
        finally:
            view._db.close()

    def begin_epoch(self) -> int:
        self._assert_writable()
        with self._lock, self._db:
            epoch = self._db.execute("SELECT COALESCE(MAX(id),-1)+1 FROM epochs").fetchone()[0]
            self._db.execute("INSERT INTO epochs(id) VALUES (?)", (epoch,))
            return epoch

    def request(self, operation: Operation, epoch: int, *, effects=(), provenance=None) -> None:
        self._assert_writable()
        if type(epoch) is not int or epoch < 0:
            raise ValueError("Epoch must be a nonnegative integer")
        body = canonical(operation.observed_row())
        effect_body = canonical(validate_effects(operation, effects))
        provenance_body = None if provenance is None else canonical(validate_git_provenance(operation, provenance))
        with self._lock, self._db:
            old = self._db.execute("SELECT body,epoch FROM requests WHERE event_id=?", (operation.event_id,)).fetchone()
            if old is not None:
                if old != (body, epoch):
                    raise ValueError("An operation identity cannot change across retries")
                expected = self._db.execute(
                    "SELECT effects,provenance FROM expectations WHERE event_id=?", (operation.event_id,)
                ).fetchone()
                if expected != (effect_body, provenance_body):
                    raise ValueError("Independent expectations cannot change across retries")
                return
            self._db.execute("INSERT OR IGNORE INTO epochs(id) VALUES (?)", (epoch,))
            if self._db.execute("SELECT closed FROM epochs WHERE id=?", (epoch,)).fetchone()[0]:
                raise ValueError("A closed epoch cannot receive more operations")
            previous = self._db.execute(
                "SELECT DISTINCT epoch FROM requests WHERE record_key=?", (operation.record_key,)
            ).fetchall()
            if previous and previous != [(epoch,)]:
                raise ValueError("Mutation epochs must use disjoint entity keys")
            self._db.execute(
                "INSERT INTO requests(event_id,tenant_id,record_key,revision,epoch,body,status) "
                "VALUES (?,?,?,?,?,?,'pending')",
                (operation.event_id, operation.tenant_id, operation.record_key, operation.client_revision, epoch, body),
            )
            self._db.execute(
                "INSERT INTO expectations(event_id,effects,provenance) VALUES (?,?,?)",
                (operation.event_id, effect_body, provenance_body),
            )

    def requested_operation(self, event_id: str, epoch: int) -> Operation:
        with self._lock:
            row = self._db.execute(
                "SELECT body FROM requests WHERE event_id=? AND epoch=?", (event_id, epoch)
            ).fetchone()
        if row is None:
            raise ValueError("Response does not belong to this requested epoch")
        return operation_from_row(json.loads(row[0]))

    def expected_effects(self, tenant_groups: dict[str, str]) -> dict[str, tuple[dict, ...]]:
        self.partition_cuts(tenant_groups)  # Validate complete, immutable routing coverage.
        result = {group: [] for group in set(tenant_groups.values())}
        with self._lock:
            rows = self._db.execute(
                "SELECT r.tenant_id,x.effects FROM expectations x JOIN requests r USING(event_id) "
                "JOIN epochs e ON e.id=r.epoch WHERE e.closed=1 AND r.status='acknowledged'"
            ).fetchall()
        for tenant, effects in rows:
            result[tenant_groups[tenant]].extend(json.loads(effects))
        return {group: tuple(sorted(rows, key=lambda row: row["effect_id"])) for group, rows in sorted(result.items())}

    def git_provenance(self) -> tuple[dict, ...]:
        with self._lock:
            rows = self._db.execute(
                "SELECT x.provenance FROM expectations x JOIN requests r USING(event_id) "
                "JOIN epochs e ON e.id=r.epoch WHERE e.closed=1 AND r.status='acknowledged' "
                "AND x.provenance IS NOT NULL ORDER BY r.rowid"
            ).fetchall()
        return tuple(json.loads(row[0]) for row in rows)

    def git_provenance_entries(self) -> tuple[dict, ...]:
        with self._lock:
            rows = self._db.execute(
                "SELECT r.body,x.provenance FROM expectations x JOIN requests r USING(event_id) "
                "JOIN epochs e ON e.id=r.epoch WHERE e.closed=1 AND r.status='acknowledged' "
                "AND x.provenance IS NOT NULL ORDER BY r.rowid"
            ).fetchall()
        return tuple({"operation": json.loads(body), "provenance": json.loads(provenance)} for body, provenance in rows)

    def acknowledge(self, event_id: str, origin: str, latency_ms: float, status_code: int) -> None:
        if not math.isfinite(latency_ms) or latency_ms < 0 or status_code not in {200, 201}:
            raise ValueError("Acknowledgment must have finite latency and a committed HTTP status")
        self._finish(event_id, "acknowledged", origin, latency_ms, status_code)

    def reject(self, event_id: str, origin: str, latency_ms: float, status_code: int) -> None:
        if not math.isfinite(latency_ms) or latency_ms < 0 or status_code not in {400, 401, 403, 404, 409, 422}:
            raise ValueError("Only a definitive application rejection resolves a request")
        self._finish(event_id, "rejected", origin, latency_ms, status_code)

    def _finish(self, event_id, status, origin, latency_ms, status_code):
        self._assert_writable()
        with self._lock, self._db:
            row = self._db.execute("SELECT status FROM requests WHERE event_id=?", (event_id,)).fetchone()
            if row is None:
                raise ValueError("No requested operation exists for this response")
            if row[0] not in {"pending", status}:
                raise ValueError("An operation cannot be both rejected and acknowledged")
            self._db.execute(
                "UPDATE requests SET status=?,origin=?,latency_ms=?,status_code=? WHERE event_id=?",
                (status, origin, latency_ms, status_code, event_id),
            )

    def close_epoch(self, epoch: int) -> None:
        self._assert_writable()
        with self._lock, self._db:
            row = self._db.execute("SELECT closed FROM epochs WHERE id=?", (epoch,)).fetchone()
            if row is None:
                raise ValueError("Cannot close an unknown epoch")
            if self._db.execute(
                "SELECT 1 FROM requests WHERE epoch=? AND status='pending' LIMIT 1", (epoch,)
            ).fetchone():
                raise RuntimeError("Epoch has unresolved request outcomes")
            self._db.execute("UPDATE epochs SET closed=1 WHERE id=?", (epoch,))

    def cut(self) -> ReceiptCut:
        return self._cut()

    def partition_cuts(self, tenant_groups: dict[str, str]) -> dict[str, ReceiptCut]:
        """Expected partition hashes come from private receipts and frozen routing."""
        with self._lock:
            groups = self.validate_routing(tenant_groups)
            return {group: self._cut(tuple(sorted(tenants))) for group, tenants in sorted(groups.items())}

    def validate_routing(self, tenant_groups: dict[str, str]) -> dict[str, list[str]]:
        """Validate routing coverage without materializing every receipt cut."""
        if type(tenant_groups) is not dict or not tenant_groups:
            raise ValueError("Tenant routing must be a nonempty mapping")
        groups = {}
        for tenant, group in tenant_groups.items():
            _identity(tenant)
            if type(group) is not str or not group or len(group) > 63:
                raise ValueError("Invalid database group identity")
            groups.setdefault(group, []).append(tenant)
        with self._lock:
            known = {row[0] for row in self._db.execute("SELECT DISTINCT tenant_id FROM requests")}
            if not known <= set(tenant_groups):
                raise ValueError("Frozen routing omits requested tenants")
            return groups

    def _cut(self, tenants: tuple[str, ...] | None = None) -> ReceiptCut:
        scope = "" if tenants is None else " AND r.tenant_id IN (" + ",".join("?" for _ in tenants) + ")"
        params = tenants or ()
        with self._lock:
            epochs = tuple(row[0] for row in self._db.execute("SELECT id FROM epochs WHERE closed=1 ORDER BY id"))
            rows = self._db.execute(
                "SELECT r.body FROM requests r JOIN epochs e ON e.id=r.epoch "
                "WHERE e.closed=1 AND r.status='acknowledged'" + scope + " ORDER BY r.event_id",
                params,
            )
            journal, count = hashlib.sha256(), 0
            for (body,) in rows:
                journal.update(body.encode() + b"\n")
                count += 1
            rows = self._db.execute(
                "SELECT r.record_key,r.body FROM requests r JOIN epochs e ON e.id=r.epoch "
                "WHERE e.closed=1 AND r.status='acknowledged' AND r.revision=("
                "SELECT MAX(other.revision) FROM requests other WHERE other.record_key=r.record_key "
                "AND other.status='acknowledged' AND other.epoch IN (SELECT id FROM epochs WHERE closed=1))"
                + scope
                + " ORDER BY r.record_key",
                params,
            )
            current, entities = hashlib.sha256(), []
            for entity_id, body in rows:
                current.update(body.encode() + b"\n")
                entities.append(entity_id)
            return ReceiptCut(epochs, count, tuple(entities), journal.hexdigest(), current.hexdigest())

    def unresolved(self) -> tuple[dict, ...]:
        with self._lock:
            return tuple(
                json.loads(row[0]) for row in self._db.execute("SELECT body FROM requests WHERE status='pending'")
            )

    def pending_requests(self) -> tuple[dict, ...]:
        """Exact private retry material; never infer a timeout's outcome from SQL."""
        with self._lock:
            rows = self._db.execute(
                "SELECT r.body,r.epoch,x.effects,x.provenance FROM requests r "
                "JOIN expectations x USING(event_id) WHERE r.status='pending' ORDER BY r.epoch,r.event_id"
            ).fetchall()
        return tuple(
            {
                "operation": json.loads(body),
                "epoch": epoch,
                "effects": json.loads(effects),
                "provenance": None if provenance is None else json.loads(provenance),
            }
            for body, epoch, effects, provenance in rows
        )

    def pending_epochs(self) -> frozenset[int]:
        """Bounded closure metadata, using the pending-status index."""
        with self._lock:
            return frozenset(
                row[0] for row in self._db.execute("SELECT DISTINCT epoch FROM requests WHERE status='pending'")
            )

    def close(self):
        with self._lock:
            self._db.close()

    def _assert_writable(self):
        if self._read_only:
            # Reject before sqlite's context manager can roll back the pinned
            # read transaction in response to a failed write.
            raise sqlite3.OperationalError("attempt to write a readonly receipt snapshot")
        used = sum(
            path.stat().st_size
            for path in (
                self._path,
                self._path.with_name(self._path.name + "-wal"),
                self._path.with_name(self._path.name + "-shm"),
            )
            if path.exists()
        )
        if (
            self.capacity_error is not None
            or used > self._byte_budget - min(2 * 1024**2, self._byte_budget // 2)
            or shutil.disk_usage(self._path.parent).free < 256 * 1024**2
        ):
            self.capacity_error = "Private receipt storage capacity is unavailable"
            raise RuntimeError(self.capacity_error)


class InvalidApplicationReceipt(RuntimeError):
    """An actual product response did not prove its requested acknowledgment."""


class WorkloadClient:
    """A retry keeps its identity; an HTTP timeout never becomes an acknowledgment."""

    def __init__(self, origin: str, token: str, ledger: ReceiptLedger, *, transport=None, verify=True, cancel=None):
        self.origin, self.ledger = origin.rstrip("/"), ledger
        self.last_rejection = None
        self.cancel, self.deadline = cancel, None
        self._client = httpx.Client(
            base_url=self.origin,
            headers={"Authorization": f"Bearer {token}", "Accept-Encoding": "identity"},
            timeout=10,
            transport=transport
            or DeadlineTransport(verify=verify, cancelled=lambda: self.cancel is not None and self.cancel.is_set()),
            verify=verify,
        )

    def submit(
        self,
        operation: Operation,
        *,
        epoch: int,
        attempts: int = 3,
        retry_delay: float = 0.25,
        effects=(),
        provenance=None,
    ) -> bool:
        if type(attempts) is not int or not 1 <= attempts <= 10 or not math.isfinite(retry_delay) or retry_delay < 0:
            raise ValueError("Invalid bounded retry policy")
        self.last_rejection = None
        self.ledger.request(operation, epoch, effects=effects, provenance=provenance)
        for attempt in range(attempts):
            if self.cancel is not None and self.cancel.is_set():
                return False
            start = time.monotonic()
            deadline = min(start + 10, self.deadline) if self.deadline is not None else start + 10
            if deadline <= start:
                return False
            try:
                content = bytearray()
                with self._client.stream(
                    "POST",
                    "/v1/operations",
                    json=operation.request(),
                    timeout=deadline - start,
                    extensions={"absolute_deadline": deadline},
                ) as response:
                    if response.headers.get("Content-Encoding", "identity") != "identity":
                        raise InvalidApplicationReceipt("Application receipt uses an unsupported content encoding")
                    for block in response.iter_bytes():
                        if time.monotonic() >= deadline or (self.cancel is not None and self.cancel.is_set()):
                            return False
                        if len(content) + len(block) > 256 * 1024:
                            raise InvalidApplicationReceipt("Application receipt exceeds bounded response capacity")
                        content.extend(block)
                if time.monotonic() >= deadline:
                    return False
                latency = (time.monotonic() - start) * 1000
                if response.status_code in {200, 201}:
                    try:
                        receipt = json.loads(content)
                    except (ValueError, UnicodeError) as error:
                        raise InvalidApplicationReceipt("Application returned an invalid committed receipt") from error
                    expected = operation.observed_row()
                    if type(receipt) is not dict or any(receipt.get(key) != value for key, value in expected.items()):
                        raise InvalidApplicationReceipt("Application returned an invalid committed receipt")
                    self.ledger.acknowledge(operation.event_id, self.origin, latency, response.status_code)
                    return True
                if response.status_code in {400, 401, 403, 404, 409, 422}:
                    self.ledger.reject(operation.event_id, self.origin, latency, response.status_code)
                    detail = None
                    if len(content) <= 4096:
                        try:
                            body = json.loads(content)
                            if type(body) is dict and type(body.get("detail")) is str:
                                detail = body["detail"][:512]
                        except ValueError:
                            pass
                    self.last_rejection = (response.status_code, detail)
                    return False
            except httpx.TransportError:
                pass
            if attempt + 1 < attempts:
                if self.cancel is None:
                    time.sleep(min(retry_delay, 10))
                elif self.cancel.wait(min(retry_delay, 10)):
                    return False
        return False  # Ledger remains pending; the epoch cannot be graded as complete.

    def close(self):
        self._client.close()
