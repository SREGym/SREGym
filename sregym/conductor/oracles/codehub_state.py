"""Independent, bounded validation of protected CodeHub business history.

Private receipt hashes anchor the complete accepted history. SQL observations
never become a new baseline. A private disk-backed spool keeps large histories
out of memory and permits relational/effect checks against the anchored journal.
"""

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit
from uuid import UUID

MAX_PAYLOAD_BYTES = 128 * 1024
DEFAULT_SPOOL_BYTES = 128 * 1024 * 1024
MIN_SPOOL_BYTES = 64 * 1024
MAX_SPOOL_BYTES = 8 * 1024 * 1024 * 1024
SPOOL_PAGE_BYTES = 4096
OPERATION_FIELDS = (
    "event_id",
    "tenant_id",
    "entity_id",
    "project_id",
    "client_revision",
    "kind",
    "payload",
    "actor_id",
)
DOMAIN_TABLES = {
    "organization": "organizations",
    "membership": "memberships",
    "project": "projects",
    "issue": "issues",
    "comment": "comments",
    "change": "changes",
    "review": "reviews",
    "repository": "repository_refs",
    "webhook": "webhooks",
}
PAYLOAD_FIELDS = {
    "organization": {"slug", "name"},
    "membership": {"user_id", "role"},
    "project": {"slug", "name", "default_ref"},
    "issue": {"title", "body", "state"},
    "comment": {"issue_id", "body"},
    "change": {"title", "head_sha", "base_sha", "head_ref", "base_ref", "state"},
    "review": {"change_id", "head_sha", "verdict", "body"},
    "repository": {"ref", "commit_sha"},
    "webhook": {"url", "events", "enabled"},
}
KINDS = frozenset(
    {
        "organization.create",
        "membership.set",
        "project.create",
        "project.update",
        "issue.create",
        "issue.update",
        "comment.create",
        "change.create",
        "change.update",
        "review.create",
        "repository.push",
        "webhook.create",
        "webhook.update",
    }
)
SEARCH_TYPES = frozenset({"project", "issue", "comment", "change", "review", "repository"})


class StateMismatch(ValueError):
    def __init__(self, reason: str, **detail):
        super().__init__(reason)
        self.reason, self.detail = reason, detail


def canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def digest(value) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def _uuid(value: str) -> str:
    try:
        if type(value) is not str or str(UUID(value)) != value:
            raise ValueError
    except (ValueError, TypeError, AttributeError) as exc:
        raise StateMismatch("malformed_observed_identity") from exc
    return value


def record_key(tenant_id: str, entity_id: str) -> str:
    return f"{_uuid(tenant_id)}/{_uuid(entity_id)}"


def _integer(value, *, minimum=0):
    if type(value) is not int or value < minimum:
        raise StateMismatch("malformed_observed_integer")
    return value


def _hash(value):
    if type(value) is not str or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise StateMismatch("malformed_observed_digest")
    return value


def _json(value):
    def pairs(items):
        result = {}
        for key, item in items:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = item
        return result

    try:
        if type(value) is str:
            if len(value.encode()) > MAX_PAYLOAD_BYTES:
                raise ValueError("Oversized JSON")
            value = json.loads(
                value,
                object_pairs_hook=pairs,
                parse_constant=lambda _value: (_ for _ in ()).throw(ValueError("Nonfinite JSON")),
            )
        if type(value) is not dict or len(canonical(value).encode()) > MAX_PAYLOAD_BYTES:
            raise ValueError("Expected bounded JSON object")
        return value
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise StateMismatch("malformed_observed_payload") from exc


def _text(payload, field, maximum, *, empty=False):
    value = payload.get(field, "" if empty else None)
    if type(value) is not str or len(value) > maximum or "\x00" in value or (not empty and not value.strip()):
        raise StateMismatch("malformed_domain_payload", field=field)
    return value


def _choice(payload, field, options):
    value = payload.get(field)
    if type(value) is not str or value not in options:
        raise StateMismatch("malformed_domain_payload", field=field)
    return value


def _git_sha(payload, field):
    value = payload.get(field)
    if type(value) is not str or not re.fullmatch(r"[0-9a-f]{40}", value):
        raise StateMismatch("malformed_domain_payload", field=field)
    return value


def _ref(payload, field):
    value = _text(payload, field, 200)
    if (
        not value.startswith("refs/heads/")
        or any(item in value for item in ("..", "@{", "\\", "//"))
        or re.search(r"[\s~^:?*\[\x00-\x1f]", value)
        or value.endswith(("/", ".", ".lock"))
    ):
        raise StateMismatch("malformed_domain_payload", field=field)
    return value


@dataclass(frozen=True)
class ProtectedReceiptCut:
    group: str
    closed_epochs: tuple[int, ...]
    operations: int
    record_keys: tuple[str, ...]
    journal_sha256: str
    current_sha256: str

    def __post_init__(self):
        if type(self.group) is not str or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", self.group):
            raise ValueError("Receipt group must be a DNS label")
        if (
            type(self.closed_epochs) is not tuple
            or any(type(epoch) is not int or epoch < 0 for epoch in self.closed_epochs)
            or tuple(sorted(set(self.closed_epochs))) != self.closed_epochs
        ):
            raise ValueError("Closed epochs must be an immutable strictly ordered tuple")
        if type(self.operations) is not int or self.operations < 1 or not self.closed_epochs:
            raise ValueError("A protected cut requires acknowledged operations and closed epochs")
        if (
            type(self.record_keys) is not tuple
            or not self.record_keys
            or any(type(key) is not str for key in self.record_keys)
            or tuple(sorted(set(self.record_keys))) != self.record_keys
        ):
            raise ValueError("Protected record keys must be an immutable strictly ordered tuple")
        for key in self.record_keys:
            if type(key) is not str or len(key.split("/")) != 2:
                raise ValueError("Protected keys must be tenant-qualified UUID identities")
            try:
                record_key(*key.split("/"))
            except StateMismatch as exc:
                raise ValueError("Protected keys must be tenant-qualified UUID identities") from exc
        if self.operations < len(self.record_keys):
            raise ValueError("Protected entities cannot outnumber accepted operations")
        for value in (self.journal_sha256, self.current_sha256):
            if type(value) is not str or not re.fullmatch(r"[0-9a-f]{64}", value):
                raise ValueError("Protected hashes must be lowercase SHA-256 digests")

    @classmethod
    def from_receipt_cut(cls, group, cut):
        return cls(
            group, tuple(cut.closed_epochs), cut.operations, tuple(cut.entities), cut.journal_sha256, cut.current_sha256
        )


@dataclass(frozen=True)
class EffectReceiptCut:
    group: str
    count: int
    sha256: str

    def __post_init__(self):
        if type(self.group) is not str or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", self.group):
            raise ValueError("Effect group must be a DNS label")
        if (
            type(self.count) is not int
            or self.count < 0
            or type(self.sha256) is not str
            or not re.fullmatch(r"[0-9a-f]{64}", self.sha256)
        ):
            raise ValueError("Effect cuts need a nonnegative count and SHA-256 digest")


@dataclass(frozen=True)
class AcceptedOperation:
    event_id: str
    tenant_id: str
    entity_id: str
    project_id: str | None
    client_revision: int
    kind: str
    payload_json: str
    actor_id: str

    @property
    def record_key(self):
        return record_key(self.tenant_id, self.entity_id)

    @property
    def entity_type(self):
        return self.kind.split(".", 1)[0]

    def request(self):
        return {name: getattr(self, name) for name in OPERATION_FIELDS if name not in {"payload", "actor_id"}} | {
            "payload": json.loads(self.payload_json)
        }

    def row(self):
        return self.request() | {"actor_id": self.actor_id}

    def document(self, creator_id):
        payload, kind = json.loads(self.payload_json), self.entity_type
        if kind in {"organization", "project"}:
            slug = _text(payload, "slug", 80)
            if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,79}", slug):
                raise StateMismatch("malformed_domain_payload", field="slug")
            document = {"slug": slug, "name": _text(payload, "name", 200)}
            if kind == "organization":
                document["owner_id"] = self.actor_id
            else:
                document.update(default_ref=_ref(payload, "default_ref"), author_id=creator_id)
            return document
        if kind == "membership":
            return {
                "user_id": _uuid(payload.get("user_id")),
                "role": _choice(payload, "role", {"owner", "admin", "developer", "viewer"}),
            }
        if kind == "issue":
            return {
                "title": _text(payload, "title", 200),
                "body": _text(payload, "body", 16000, empty=True),
                "state": _choice(payload, "state", {"open", "closed"}),
                "author_id": creator_id,
            }
        if kind == "comment":
            return {
                "issue_id": _uuid(payload.get("issue_id")),
                "body": _text(payload, "body", 16000),
                "author_id": self.actor_id,
            }
        if kind == "change":
            return {
                "title": _text(payload, "title", 200),
                "head_sha": _git_sha(payload, "head_sha"),
                "base_sha": _git_sha(payload, "base_sha"),
                "head_ref": _ref(payload, "head_ref"),
                "base_ref": _ref(payload, "base_ref"),
                "state": _choice(payload, "state", {"open", "closed", "merged"}),
                "author_id": creator_id,
            }
        if kind == "review":
            return {
                "change_id": _uuid(payload.get("change_id")),
                "head_sha": _git_sha(payload, "head_sha"),
                "verdict": _choice(payload, "verdict", {"approve", "request_changes", "comment"}),
                "body": _text(payload, "body", 16000, empty=True),
                "author_id": self.actor_id,
            }
        if kind == "repository":
            return {
                "ref": _ref(payload, "ref"),
                "commit_sha": _git_sha(payload, "commit_sha"),
                "author_id": self.actor_id,
            }
        if kind == "webhook":
            url = _text(payload, "url", 2048)
            parsed = urlsplit(url)
            events, enabled = payload.get("events"), payload.get("enabled", True)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or type(enabled) is not bool
                or type(events) is not list
                or not events
                or any(type(event) is not str or event not in KINDS for event in events)
            ):
                raise StateMismatch("malformed_domain_payload", field="webhook")
            return {"url": url, "events": sorted(set(events)), "enabled": enabled, "author_id": creator_id}
        raise StateMismatch("malformed_domain_payload")


def operation_from_row(row) -> AcceptedOperation:
    try:
        for field in ("event_id", "tenant_id", "entity_id", "actor_id"):
            _uuid(row[field])
        if row["project_id"] is not None:
            _uuid(row["project_id"])
        _integer(row["client_revision"], minimum=1)
        if type(row["kind"]) is not str or row["kind"] not in KINDS:
            raise StateMismatch("malformed_operation_kind")
        payload = _json(row["payload"])
        kind = row["kind"].split(".", 1)[0]
        if not set(payload) <= PAYLOAD_FIELDS[kind]:
            raise StateMismatch("malformed_domain_payload")
        op = AcceptedOperation(
            **{name: row[name] for name in OPERATION_FIELDS if name != "payload"}, payload_json=canonical(payload)
        )
        if row.get("record_key", op.record_key) != op.record_key:
            raise StateMismatch("record_identity_mismatch")
        if "payload_sha256" in row and row["payload_sha256"] != digest(op.row()):
            raise StateMismatch("operation_digest_mismatch")
        if op.kind.endswith(".create") and op.client_revision != 1:
            raise StateMismatch("invalid_creation_revision")
        if kind == "organization" and (
            op.entity_id != op.tenant_id or op.project_id is not None or op.client_revision != 1
        ):
            raise StateMismatch("invalid_tenant_root")
        if kind == "membership" and (op.entity_id != payload.get("user_id") or op.project_id is not None):
            raise StateMismatch("invalid_membership_identity")
        if kind == "project" and op.entity_id != op.project_id:
            raise StateMismatch("invalid_project_identity")
        if kind not in {"organization", "membership", "project"} and op.project_id is None:
            raise StateMismatch("missing_project_relationship")
        op.document(op.actor_id)  # Validate typed public payload semantics, without observing current data.
        return op
    except (KeyError, TypeError) as exc:
        raise StateMismatch("malformed_operation_row") from exc


def effect_identity(event_id, kind, destination):
    return hashlib.sha256(f"{event_id}/{kind}/{destination}".encode()).hexdigest()


class _SpoolCursor(sqlite3.Cursor):
    def execute(self, *args, **kwargs):
        return self.connection._bounded_call(super().execute, *args, **kwargs)

    def executemany(self, *args, **kwargs):
        return self.connection._bounded_call(super().executemany, *args, **kwargs)

    def executescript(self, *args, **kwargs):
        return self.connection._bounded_call(super().executescript, *args, **kwargs)


class _SpoolConnection(sqlite3.Connection):
    """A full disposable spool closes immediately and cannot become a baseline."""

    _capacity_failed = False

    def _bounded_call(self, function, *args, **kwargs):
        if self._capacity_failed:
            raise StateMismatch("verification_spool_capacity_exceeded")
        try:
            return function(*args, **kwargs)
        except sqlite3.Error as exc:
            if getattr(exc, "sqlite_errorcode", -1) & 255 != sqlite3.SQLITE_FULL:
                raise
            self._capacity_failed = True
            try:
                super().close()
            except sqlite3.Error as cleanup_error:
                exc.add_note(f"Full disposable spool also failed to close: {cleanup_error}")
            raise StateMismatch("verification_spool_capacity_exceeded") from exc

    def cursor(self):
        return self._bounded_call(super().cursor, _SpoolCursor)

    def execute(self, *args, **kwargs):
        return self.cursor().execute(*args, **kwargs)

    def executemany(self, *args, **kwargs):
        return self.cursor().executemany(*args, **kwargs)

    def executescript(self, *args, **kwargs):
        return self.cursor().executescript(*args, **kwargs)

    def commit(self):
        return self._bounded_call(super().commit)


class ProtectedState:
    """Exact observation spool with a trusted per-instance disk budget.

    Callers divide their declared aggregate scratch budget over concurrently
    live instances. The main SQLite file is page-capped; disposable observations
    use no rollback journal, WAL, or on-disk temporary database. Filesystem
    metadata is outside the byte cap. Memory temporary work remains subject to
    the verifier container's memory limit, and any capacity failure is fatal.
    """

    def __init__(
        self, cut: ProtectedReceiptCut, *, relationship_state=None, spool_bytes=DEFAULT_SPOOL_BYTES, scratch_dir=None
    ):
        if type(cut) is not ProtectedReceiptCut:
            raise ValueError("ProtectedState needs an immutable private receipt cut")
        if type(spool_bytes) is not int or not MIN_SPOOL_BYTES <= spool_bytes <= MAX_SPOOL_BYTES:
            raise ValueError("A trusted spool budget must be between 64 KiB and 8 GiB")
        if scratch_dir is not None:
            scratch_dir = Path(scratch_dir)
            if not scratch_dir.is_absolute() or scratch_dir.is_symlink() or not scratch_dir.is_dir():
                raise ValueError("Verifier scratch must be an existing absolute trusted directory")
        self.cut, self.keys = cut, frozenset(cut.record_keys)
        self.relationship_state = relationship_state
        self.spool_bytes = spool_bytes
        self._row_counts = {}
        self._directory = TemporaryDirectory(prefix="history-", dir=scratch_dir)
        path = Path(self._directory.name) / "observed.sqlite"
        self.db = None
        try:
            self.db = sqlite3.connect(path, factory=_SpoolConnection)
            path.chmod(0o600)
            self.db.executescript(
                f"PRAGMA page_size={SPOOL_PAGE_BYTES}; PRAGMA max_page_count={spool_bytes // SPOOL_PAGE_BYTES};"
                "PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF; PRAGMA temp_store=MEMORY;"
                "PRAGMA cache_size=-16384; PRAGMA mmap_size=0;"
                "CREATE TABLE journal(event_id TEXT PRIMARY KEY,record_key TEXT NOT NULL,revision INTEGER NOT NULL,body TEXT NOT NULL,UNIQUE(record_key,revision));"
                "CREATE INDEX latest_revision ON journal(record_key,revision DESC);"
                "CREATE TABLE entities(record_key TEXT PRIMARY KEY,body TEXT NOT NULL);"
                "CREATE TABLE domain(record_key TEXT PRIMARY KEY,body TEXT NOT NULL);"
                "CREATE TABLE effects(effect_id TEXT PRIMARY KEY,event_id TEXT NOT NULL,kind TEXT NOT NULL,destination TEXT NOT NULL,body TEXT NOT NULL,UNIQUE(event_id,kind,destination));"
                "CREATE TABLE builds(effect_id TEXT PRIMARY KEY,body TEXT NOT NULL);"
            )
            if self.db.execute("PRAGMA max_page_count").fetchone()[0] != spool_bytes // SPOOL_PAGE_BYTES:
                raise RuntimeError("SQLite did not enforce the declared observation page budget")
        except BaseException:
            self.close()
            raise
        self.history_anchored = False

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()

    def close(self):
        if self.db is not None:
            self.db.close()
        self._directory.cleanup()

    def _insert(self, table, sql, values, *, limit, duplicate_reason, excess_reason, duplicate_query):
        count = self._row_counts.get(table, 0)
        if count >= limit:
            query, parameters = duplicate_query
            if self.db.execute(query, parameters).fetchone() is not None:
                raise StateMismatch(duplicate_reason)
            raise StateMismatch(excess_reason, observed_rows=count + 1, required_rows=limit)
        try:
            self.db.execute(sql, values)
        except sqlite3.IntegrityError as exc:
            raise StateMismatch(duplicate_reason) from exc
        self._row_counts[table] = count + 1

    @staticmethod
    def _stream_hash(rows):
        checksum, count = hashlib.sha256(), 0
        for (body,) in rows:
            checksum.update(body.encode() + b"\n")
            count += 1
        return count, checksum.hexdigest()

    def load_journal(self, rows):
        for row in rows:
            try:
                key = record_key(row["tenant_id"], row["entity_id"])
            except (KeyError, TypeError) as exc:
                raise StateMismatch("malformed_operation_row") from exc
            if key not in self.keys:
                continue
            op = operation_from_row(row)
            self._insert(
                "journal",
                "INSERT INTO journal VALUES(?,?,?,?)",
                (op.event_id, key, op.client_revision, canonical(op.row())),
                limit=self.cut.operations,
                duplicate_reason="duplicate_history_identity",
                excess_reason="accepted_history_mismatch",
                duplicate_query=(
                    "SELECT 1 FROM journal WHERE event_id=? OR (record_key=? AND revision=?) LIMIT 1",
                    (op.event_id, key, op.client_revision),
                ),
            )
        self.db.commit()
        count, checksum = self._stream_hash(self.db.execute("SELECT body FROM journal ORDER BY event_id"))
        if count != self.cut.operations or checksum != self.cut.journal_sha256:
            raise StateMismatch(
                "accepted_history_mismatch", observed_operations=count, required_operations=self.cut.operations
            )
        count, checksum = self._stream_hash(
            self.db.execute(
                "SELECT j.body FROM journal j WHERE j.revision=(SELECT MAX(v.revision) FROM journal v WHERE v.record_key=j.record_key) ORDER BY j.record_key"
            )
        )
        if count != len(self.keys) or checksum != self.cut.current_sha256:
            raise StateMismatch("canonical_history_mismatch")
        self.history_anchored = True

    def latest(self):
        if not self.history_anchored:
            raise RuntimeError("Compare the immutable receipt cut before deriving public projections")
        for key, body in self.db.execute(
            "SELECT j.record_key,j.body FROM journal j WHERE j.revision=(SELECT MAX(v.revision) FROM journal v WHERE v.record_key=j.record_key) ORDER BY j.record_key"
        ):
            op = operation_from_row(json.loads(body))
            earliest = self.db.execute(
                "SELECT body FROM journal WHERE record_key=? ORDER BY revision LIMIT 1", (key,)
            ).fetchone()
            first = operation_from_row(json.loads(earliest[0]))
            if first.client_revision != 1 or first.entity_type != op.entity_type or first.project_id != op.project_id:
                raise StateMismatch("invalid_entity_history")
            yield op, op.document(first.actor_id)

    def check_entities(self, rows):
        for row in rows:
            key = record_key(row["tenant_id"], row["id"])
            if key not in self.keys:
                continue
            body = {name: row[name] for name in ("tenant_id", "id", "project_id", "entity_type", "revision")}
            body["document"] = _json(row["document"])
            _integer(body["revision"], minimum=1)
            self._insert(
                "entities",
                "INSERT INTO entities VALUES(?,?)",
                (key, canonical(body)),
                limit=len(self.keys),
                duplicate_reason="duplicate_current_record",
                excess_reason="current_record_mismatch",
                duplicate_query=("SELECT 1 FROM entities WHERE record_key=?", (key,)),
            )
        for op, document in self.latest():
            observed = self.db.execute("SELECT body FROM entities WHERE record_key=?", (op.record_key,)).fetchone()
            expected = {
                "tenant_id": op.tenant_id,
                "id": op.entity_id,
                "project_id": op.project_id,
                "entity_type": op.entity_type,
                "revision": op.client_revision,
                "document": document,
            }
            if observed is None or observed[0] != canonical(expected):
                raise StateMismatch("current_record_mismatch")

    def check_domain_rows(self, table, rows):
        if table not in DOMAIN_TABLES.values():
            raise ValueError("Unsupported domain table")
        for row in rows:
            if table == "organizations":
                key = record_key(row["id"], row["id"])
            elif table == "memberships":
                key = record_key(row["tenant_id"], row["user_id"])
            else:
                key = record_key(row["tenant_id"], row["id"])
            if key not in self.keys:
                continue
            latest = self.db.execute(
                "SELECT body FROM journal WHERE record_key=? ORDER BY revision DESC LIMIT 1", (key,)
            ).fetchone()
            if latest is None or DOMAIN_TABLES[operation_from_row(json.loads(latest[0])).entity_type] != table:
                raise StateMismatch("domain_entity_type_mismatch")
            clean = {name: value for name, value in row.items() if name != "created_at"}
            if table == "webhooks":
                if type(clean["enabled"]) is int and clean["enabled"] in {0, 1}:
                    clean["enabled"] = bool(clean["enabled"])
                if type(clean["events"]) is str:
                    try:
                        clean["events"] = json.loads(clean["events"])
                    except (ValueError, TypeError) as exc:
                        raise StateMismatch("malformed_domain_payload") from exc
            self._insert(
                "domain",
                "INSERT INTO domain VALUES(?,?)",
                (key, canonical(clean)),
                limit=len(self.keys),
                duplicate_reason="duplicate_domain_record",
                excess_reason="relational_projection_mismatch",
                duplicate_query=("SELECT 1 FROM domain WHERE record_key=?", (key,)),
            )

    def verify_domain(self):
        for op, document in self.latest():
            expected = document.copy()
            if op.entity_type == "membership":
                expected["tenant_id"] = op.tenant_id
            else:
                expected["id"] = op.entity_id
                if op.entity_type != "organization":
                    expected["tenant_id"] = op.tenant_id
                    if op.entity_type != "project":
                        expected["project_id"] = op.project_id
            observed = self.db.execute("SELECT body FROM domain WHERE record_key=?", (op.record_key,)).fetchone()
            if observed is None or observed[0] != canonical(expected):
                raise StateMismatch("relational_projection_mismatch")
            if op.project_id and op.entity_type != "project":
                self._relationship(op, op.project_id, "project")
            if op.entity_type == "comment":
                self._relationship(op, document["issue_id"], "issue")
            if op.entity_type == "review":
                self._relationship(op, document["change_id"], "change")

    def _relationship(self, op, target_id, kind):
        target = self.db.execute(
            "SELECT body FROM journal WHERE record_key=? ORDER BY revision DESC LIMIT 1",
            (record_key(op.tenant_id, target_id),),
        ).fetchone()
        if target is None and self.relationship_state is not None and self.relationship_state.history_anchored:
            target = self.relationship_state.db.execute(
                "SELECT body FROM journal WHERE record_key=? ORDER BY revision DESC LIMIT 1",
                (record_key(op.tenant_id, target_id),),
            ).fetchone()
        if target is None:
            raise StateMismatch("missing_protected_relationship")
        related = operation_from_row(json.loads(target[0]))
        if related.entity_type != kind or (kind != "project" and related.project_id != op.project_id):
            raise StateMismatch("invalid_tenant_relationship")

    def load_effects(self, rows, cut: EffectReceiptCut):
        if type(cut) is not EffectReceiptCut or cut.group != self.cut.group:
            raise ValueError("Expected effects must come from the same private receipt group")
        for row in rows:
            event = self.db.execute("SELECT body FROM journal WHERE event_id=?", (row["event_id"],)).fetchone()
            if event is None:
                continue
            op = operation_from_row(json.loads(event[0]))
            kind, destination, payload = row["effect_kind"], row["destination"], _json(row["payload"])
            _hash(row["effect_id"])
            if (
                type(kind) is not str
                or type(destination) is not str
                or kind not in {"search", "build", "delivery"}
                or row["effect_id"] != effect_identity(op.event_id, kind, destination)
            ):
                raise StateMismatch("effect_identity_mismatch")
            if kind == "search":
                expected = op.request()
                valid = op.entity_type in SEARCH_TYPES and destination == op.entity_id
            elif kind == "build":
                commit = json.loads(op.payload_json).get("commit_sha") or json.loads(op.payload_json).get("head_sha")
                expected = op.request() | {"commit_sha": commit}
                valid = op.kind in {"repository.push", "change.create", "change.update"} and destination == (
                    op.project_id or op.entity_id
                )
            else:
                _uuid(destination)
                expected = {"operation": op.request(), "url": payload.get("url")}
                parsed = urlsplit(payload.get("url", ""))
                valid = (
                    parsed.scheme in {"http", "https"}
                    and parsed.hostname
                    and not parsed.username
                    and not parsed.password
                )
            if not valid or canonical(payload) != canonical(expected):
                raise StateMismatch("effect_payload_mismatch")
            if row["state"] != "done":
                raise StateMismatch("required_backlog_pending")
            _integer(row.get("attempts", 0))
            body = {name: row[name] for name in ("effect_id", "event_id", "effect_kind", "destination")}
            body["payload"] = payload
            self._insert(
                "effects",
                "INSERT INTO effects VALUES(?,?,?,?,?)",
                (row["effect_id"], op.event_id, kind, destination, canonical(body)),
                limit=cut.count,
                duplicate_reason="duplicate_effect_identity",
                excess_reason="business_effect_history_mismatch",
                duplicate_query=("SELECT 1 FROM effects WHERE effect_id=?", (row["effect_id"],)),
            )
        for (body,) in self.db.execute("SELECT body FROM journal"):
            op = operation_from_row(json.loads(body))
            mandatory = []
            if op.entity_type in SEARCH_TYPES:
                mandatory.append(("search", op.entity_id))
            if op.kind in {"repository.push", "change.create", "change.update"}:
                mandatory.append(("build", op.project_id or op.entity_id))
            for kind, destination in mandatory:
                if (
                    self.db.execute(
                        "SELECT 1 FROM effects WHERE effect_id=?", (effect_identity(op.event_id, kind, destination),)
                    ).fetchone()
                    is None
                ):
                    raise StateMismatch("required_business_effect_missing", effect_kind=kind)
        count, checksum = self._stream_hash(self.db.execute("SELECT body FROM effects ORDER BY effect_id"))
        if count != cut.count or checksum != cut.sha256:
            raise StateMismatch("business_effect_history_mismatch")

    def check_build_rows(self, rows):
        required = self.db.execute("SELECT COUNT(*) FROM effects WHERE kind='build'").fetchone()[0]
        for row in rows:
            effect = self.db.execute(
                "SELECT body FROM effects WHERE effect_id=? AND kind='build'", (row["effect_id"],)
            ).fetchone()
            if effect is None:
                continue
            payload = json.loads(effect[0])["payload"]
            if (
                row["event_id"] != payload["event_id"]
                or row["project_id"] != payload["project_id"]
                or row["commit_sha"] != payload["commit_sha"]
            ):
                raise StateMismatch("build_identity_mismatch")
            _hash(row["artifact_sha256"])
            _integer(row["file_count"], minimum=1)
            clean = {
                name: row[name]
                for name in ("effect_id", "event_id", "project_id", "commit_sha", "artifact_sha256", "file_count")
            }
            self._insert(
                "builds",
                "INSERT INTO builds VALUES(?,?)",
                (row["effect_id"], canonical(clean)),
                limit=required,
                duplicate_reason="duplicate_build_identity",
                excess_reason="build_identity_mismatch",
                duplicate_query=("SELECT 1 FROM builds WHERE effect_id=?", (row["effect_id"],)),
            )
        missing = self.db.execute(
            "SELECT 1 FROM effects e LEFT JOIN builds b USING(effect_id) WHERE e.kind='build' AND b.effect_id IS NULL LIMIT 1"
        ).fetchone()
        if missing:
            raise StateMismatch("build_outcome_missing")

    def effect_rows(self, kind):
        for (body,) in self.db.execute("SELECT body FROM effects WHERE kind=? ORDER BY effect_id", (kind,)):
            yield json.loads(body)

    def build_rows(self):
        for (body,) in self.db.execute("SELECT body FROM builds ORDER BY effect_id"):
            yield json.loads(body)

    def check_delivery_receipts(self, receipts):
        self.db.execute("CREATE TABLE IF NOT EXISTS delivery_receipts(effect_id TEXT PRIMARY KEY)")
        self.db.execute("DELETE FROM delivery_receipts")
        self._row_counts["delivery_receipts"] = 0
        required = self.db.execute("SELECT COUNT(*) FROM effects WHERE kind='delivery'").fetchone()[0]
        for receipt in receipts:
            effect_id = receipt.get("effect_id")
            effect = self.db.execute(
                "SELECT body FROM effects WHERE effect_id=? AND kind='delivery'", (effect_id,)
            ).fetchone()
            if effect is None:
                raise StateMismatch("unexpected_delivery_receipt")
            expected = json.loads(effect[0])
            if receipt.get("event_id") != expected["event_id"] or canonical(receipt.get("operation")) != canonical(
                expected["payload"]["operation"]
            ):
                raise StateMismatch("delivery_payload_mismatch")
            if type(receipt.get("application_count")) is not int or receipt["application_count"] != 1:
                raise StateMismatch("duplicate_delivery_business_effect")
            _integer(receipt.get("attempt_count"), minimum=1)
            self._insert(
                "delivery_receipts",
                "INSERT INTO delivery_receipts VALUES(?)",
                (effect_id,),
                limit=required,
                duplicate_reason="unexpected_delivery_receipt",
                excess_reason="unexpected_delivery_receipt",
                duplicate_query=("SELECT 1 FROM delivery_receipts WHERE effect_id=?", (effect_id,)),
            )
        if (
            self.db.execute("SELECT COUNT(*) FROM delivery_receipts").fetchone()[0]
            != self.db.execute("SELECT COUNT(*) FROM effects WHERE kind='delivery'").fetchone()[0]
        ):
            raise StateMismatch("independent_delivery_receipt_missing")
