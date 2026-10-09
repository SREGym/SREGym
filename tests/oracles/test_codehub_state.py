"""Recovery negative controls anchored to independently captured receipts."""

import hashlib
import sqlite3
from dataclasses import replace
from pathlib import Path
from uuid import UUID

import pytest

from sregym.conductor.oracles import codehub_state
from sregym.conductor.oracles.codehub_state import (
    DOMAIN_TABLES,
    EffectReceiptCut,
    ProtectedReceiptCut,
    ProtectedState,
    StateMismatch,
    canonical,
    digest,
    effect_identity,
    operation_from_row,
)


def uid(number):
    return str(UUID(int=number))


def operation(number, entity, revision, kind, payload, *, tenant=100, project=200, actor=1):
    row = {
        "event_id": uid(number),
        "tenant_id": uid(tenant),
        "entity_id": uid(entity),
        "project_id": uid(project) if project is not None else None,
        "client_revision": revision,
        "kind": kind,
        "payload": payload,
        "actor_id": uid(actor),
    }
    row.update(record_key=f"{row['tenant_id']}/{row['entity_id']}", payload_sha256=digest(row))
    # The digest covers only the operation envelope plus actor, not redundant SQL metadata.
    row["payload_sha256"] = digest(
        {name: value for name, value in row.items() if name not in {"record_key", "payload_sha256"}}
    )
    return row


def stream_hash(rows):
    checksum = hashlib.sha256()
    for row in rows:
        checksum.update(canonical(row).encode() + b"\n")
    return checksum.hexdigest()


def receipt_cut(rows):
    accepted = [operation_from_row(row) for row in rows]
    current = {}
    for op in accepted:
        if op.record_key not in current or current[op.record_key].client_revision < op.client_revision:
            current[op.record_key] = op
    return ProtectedReceiptCut(
        "group-a",
        (0, 1),
        len(rows),
        tuple(sorted(current)),
        stream_hash(op.row() for op in sorted(accepted, key=lambda op: op.event_id)),
        stream_hash(current[key].row() for key in sorted(current)),
    )


def effects_for(rows, *, delivery=False):
    effects = []
    for row in rows:
        op = operation_from_row(row)
        if op.entity_type in {"project", "issue", "comment", "change", "review", "repository"}:
            entries = [("search", op.entity_id, op.request())]
            if op.kind == "repository.push":
                entries.append(
                    ("build", op.project_id, op.request() | {"commit_sha": op.request()["payload"]["commit_sha"]})
                )
            if delivery and op.entity_type == "issue":
                entries.append(
                    ("delivery", uid(900), {"operation": op.request(), "url": "http://customer-webhook/deliveries"})
                )
            for kind, destination, payload in entries:
                effects.append(
                    {
                        "effect_id": effect_identity(op.event_id, kind, destination),
                        "event_id": op.event_id,
                        "effect_kind": kind,
                        "destination": destination,
                        "payload": payload,
                        "state": "done",
                        "attempts": 1,
                    }
                )
    private = [
        {name: value for name, value in effect.items() if name not in {"state", "attempts"}} for effect in effects
    ]
    cut = EffectReceiptCut(
        "group-a", len(private), stream_hash(sorted(private, key=lambda effect: effect["effect_id"]))
    )
    return effects, cut


@pytest.fixture
def rows():
    return [
        operation(
            10, 100, 1, "organization.create", {"slug": "northwind", "name": "Northwind Engineering"}, project=None
        ),
        operation(
            11, 200, 1, "project.create", {"slug": "gateway", "name": "Gateway", "default_ref": "refs/heads/main"}
        ),
        operation(12, 300, 1, "issue.create", {"title": "Retry delivery", "state": "open"}),
        operation(
            14,
            300,
            3,
            "issue.update",
            {"title": "Retry delivery resolved", "body": "Updated", "state": "closed"},
            actor=2,
        ),
        operation(13, 300, 2, "issue.update", {"title": "Retry delivery investigated", "state": "open"}),
        operation(15, 400, 1, "comment.create", {"issue_id": uid(300), "body": "Reviewed the logs"}),
        operation(16, 500, 1, "repository.push", {"ref": "refs/heads/main", "commit_sha": "a" * 40}),
    ]


def projection_rows(state):
    return [
        {
            "tenant_id": op.tenant_id,
            "id": op.entity_id,
            "project_id": op.project_id,
            "entity_type": op.entity_type,
            "revision": op.client_revision,
            "document": doc,
        }
        for op, doc in state.latest()
    ]


def domain_rows(state):
    tables = {name: [] for name in DOMAIN_TABLES.values()}
    for op, document in state.latest():
        row = document.copy()
        if op.entity_type == "membership":
            row["tenant_id"] = op.tenant_id
        else:
            row["id"] = op.entity_id
            if op.entity_type != "organization":
                row["tenant_id"] = op.tenant_id
                if op.entity_type != "project":
                    row["project_id"] = op.project_id
        tables[DOMAIN_TABLES[op.entity_type]].append(row)
    return tables


def test_complete_anchored_history_keeps_lower_revisions_and_original_authorship(rows):
    with ProtectedState(receipt_cut(rows)) as state:
        state.load_journal(iter(reversed(rows)))
        current = projection_rows(state)
        issue = next(row for row in current if row["id"] == uid(300))
        assert issue["revision"] == 3
        assert issue["document"]["author_id"] == uid(1)
        state.check_entities(iter(current))
        for table, values in domain_rows(state).items():
            state.check_domain_rows(table, iter(values))
        state.verify_domain()
        effects, cut = effects_for(rows, delivery=True)
        state.load_effects(iter(effects), cut)
        build = next(effect for effect in effects if effect["effect_kind"] == "build")
        state.check_build_rows(
            [
                {
                    "effect_id": build["effect_id"],
                    "event_id": build["event_id"],
                    "project_id": uid(200),
                    "commit_sha": "a" * 40,
                    "artifact_sha256": "b" * 64,
                    "file_count": 2,
                }
            ]
        )
        state.check_delivery_receipts(
            {
                "effect_id": effect["effect_id"],
                "event_id": effect["event_id"],
                "operation": effect["payload"]["operation"],
                "application_count": 1,
                "attempt_count": 3,
            }
            for effect in effects
            if effect["effect_kind"] == "delivery"
        )


def test_missing_noncurrent_acknowledgment_cannot_pass_a_latest_only_check(rows):
    with ProtectedState(receipt_cut(rows)) as state, pytest.raises(StateMismatch, match="accepted_history_mismatch"):
        state.load_journal(row for row in rows if row["client_revision"] != 2)


def test_fabricated_higher_revision_cannot_replace_authentic_current_state(rows):
    fabricated = operation(99, 300, 99, "issue.update", {"title": "Everything resolved", "state": "closed"})
    with ProtectedState(receipt_cut(rows)) as state, pytest.raises(StateMismatch, match="accepted_history_mismatch"):
        state.load_journal([*rows, fabricated])


@pytest.mark.parametrize("duplicate", ["event", "revision"])
def test_duplicate_event_or_record_revision_cannot_disappear_during_hashing(rows, duplicate):
    extra = rows[-1].copy()
    if duplicate == "revision":
        extra["event_id"] = uid(999)
        extra["payload_sha256"] = digest(
            {
                name: extra[name]
                for name in (
                    "event_id",
                    "tenant_id",
                    "entity_id",
                    "project_id",
                    "client_revision",
                    "kind",
                    "payload",
                    "actor_id",
                )
            }
        )
    with ProtectedState(receipt_cut(rows)) as state, pytest.raises(StateMismatch, match="duplicate_history_identity"):
        state.load_journal([*rows, extra])


@pytest.mark.parametrize(
    "change",
    [
        lambda row: row.update(actor_id=uid(77)),
        lambda row: row.update(record_key=f"{uid(101)}/{row['entity_id']}"),
        lambda row: row.update(payload='{"title":"a","title":"b","state":"open"}'),
    ],
)
def test_observed_metadata_or_malformed_json_cannot_forge_an_acknowledgment(rows, change):
    altered = [row.copy() for row in rows]
    change(altered[2])
    with ProtectedState(receipt_cut(rows)) as state, pytest.raises(StateMismatch):
        state.load_journal(altered)


def test_mutable_entities_cannot_become_a_replacement_baseline(rows):
    with ProtectedState(receipt_cut(rows)) as state:
        state.load_journal(rows)
        observed = projection_rows(state)
        issue = next(row for row in observed if row["id"] == uid(300))
        issue["document"] = {"title": "Recovered", "body": "", "state": "closed", "author_id": uid(1)}
        with pytest.raises(StateMismatch, match="current_record_mismatch"):
            state.check_entities(observed)


def test_valid_json_in_the_wrong_project_is_a_relational_failure(rows):
    with ProtectedState(receipt_cut(rows)) as state:
        state.load_journal(rows)
        observed = domain_rows(state)
        observed["comments"][0]["project_id"] = uid(201)
        for table, values in observed.items():
            state.check_domain_rows(table, values)
        with pytest.raises(StateMismatch, match="relational_projection_mismatch"):
            state.verify_domain()


def test_empty_broker_and_missing_outbox_work_cannot_pass(rows):
    with ProtectedState(receipt_cut(rows)) as state:
        state.load_journal(rows)
        effects, expected = effects_for(rows)
        with pytest.raises(StateMismatch, match="required_business_effect_missing"):
            state.load_effects(effects[:-1], expected)


def test_done_flags_without_received_business_effects_cannot_pass(rows):
    with ProtectedState(receipt_cut(rows)) as state:
        state.load_journal(rows)
        effects, expected = effects_for(rows, delivery=True)
        state.load_effects(effects, expected)
        with pytest.raises(StateMismatch, match="independent_delivery_receipt_missing"):
            state.check_delivery_receipts(())


@pytest.mark.parametrize(
    "mutation",
    [
        lambda row: row.update(effect_id="c" * 64),
        lambda row: row.update(destination=uid(123)),
        lambda row: row.update(payload={"state": "done"}),
        lambda row: row.update(state="published"),
    ],
)
def test_effect_identity_payload_and_completion_are_independent_checks(rows, mutation):
    with ProtectedState(receipt_cut(rows)) as state:
        state.load_journal(rows)
        effects, expected = effects_for(rows)
        mutation(effects[0])
        with pytest.raises(StateMismatch):
            state.load_effects(effects, expected)


def test_delivery_retries_are_allowed_but_duplicate_business_application_is_not(rows):
    with ProtectedState(receipt_cut(rows)) as state:
        state.load_journal(rows)
        effects, expected = effects_for(rows, delivery=True)
        state.load_effects(effects, expected)
        receipts = [
            {
                "effect_id": effect["effect_id"],
                "event_id": effect["event_id"],
                "operation": effect["payload"]["operation"],
                "application_count": 1,
                "attempt_count": 7,
            }
            for effect in effects
            if effect["effect_kind"] == "delivery"
        ]
        state.check_delivery_receipts(iter(receipts))
        receipts[0]["application_count"] = 2
        with pytest.raises(StateMismatch, match="duplicate_delivery_business_effect"):
            state.check_delivery_receipts(iter(receipts))


def test_source_rows_are_spooled_and_removed_on_exit(rows):
    state = ProtectedState(receipt_cut(rows))
    directory = state._directory.name
    with state:
        state.load_journal(row for row in rows)
    from pathlib import Path

    assert not Path(directory).exists()


@pytest.mark.parametrize(
    "field,value",
    [
        ("operations", True),
        ("record_keys", []),
        ("closed_epochs", (2, 1)),
        ("journal_sha256", "A" * 64),
        ("record_keys", (["bad"],)),
    ],
)
def test_malformed_private_receipt_cuts_are_configuration_errors(rows, field, value):
    with pytest.raises(ValueError):
        replace(receipt_cut(rows), **{field: value})


def test_trusted_scratch_uses_page_cap_without_disk_journal_or_temp_sidecars(rows, tmp_path):
    budget = 256 * 1024 + 127
    with ProtectedState(receipt_cut(rows), spool_bytes=budget, scratch_dir=tmp_path) as state:
        directory = Path(state._directory.name)
        assert directory.parent == tmp_path
        assert state.db.execute("PRAGMA page_size").fetchone()[0] == 4096
        assert state.db.execute("PRAGMA max_page_count").fetchone()[0] == budget // 4096
        assert state.db.execute("PRAGMA journal_mode").fetchone()[0] == "off"
        assert state.db.execute("PRAGMA temp_store").fetchone()[0] == 2
        assert state.db.execute("PRAGMA mmap_size").fetchone()[0] == 0
        state.load_journal(rows)
        state.check_entities(projection_rows(state))
        for table, values in domain_rows(state).items():
            state.check_domain_rows(table, values)
        state.verify_domain()
        effects, cut = effects_for(rows, delivery=True)
        state.load_effects(effects, cut)
        assert [path.name for path in directory.iterdir()] == ["observed.sqlite"]
        assert (directory / "observed.sqlite").stat().st_size <= budget
    assert not directory.exists()


def test_legitimate_large_history_row_exhausts_capacity_without_later_partial_pass(tmp_path):
    row = operation(10, 300, 1, "issue.create", {"title": "Capacity control", "body": "x" * 16000, "state": "open"})
    state = ProtectedState(receipt_cut([row]), spool_bytes=64 * 1024, scratch_dir=tmp_path)
    directory = Path(state._directory.name)
    try:
        with pytest.raises(StateMismatch, match="verification_spool_capacity_exceeded"):
            state.load_journal([row])
        assert not state.history_anchored
        assert (directory / "observed.sqlite").stat().st_size <= state.spool_bytes
        assert [path.name for path in directory.iterdir()] == ["observed.sqlite"]
        with pytest.raises(StateMismatch, match="verification_spool_capacity_exceeded"):
            state.db.execute("SELECT 1")
        # Bypassing the Python guard still finds an actually closed connection.
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            sqlite3.Connection.execute(state.db, "SELECT 1")
    finally:
        state.close()
        state.close()
    assert not directory.exists()


@pytest.mark.parametrize("method", ["execute", "cursor", "executemany", "executescript"])
def test_sqlite_allocation_entrypoints_share_the_fatal_disk_cap(rows, tmp_path, method):
    with ProtectedState(receipt_cut(rows), spool_bytes=64 * 1024, scratch_dir=tmp_path) as state:
        sql = "INSERT INTO entities VALUES('allocation-control',zeroblob(65536))"
        with pytest.raises(StateMismatch, match="verification_spool_capacity_exceeded"):
            if method == "cursor":
                state.db.cursor().execute(sql)
            elif method == "executemany":
                state.db.executemany(sql, [()])
            elif method == "executescript":
                state.db.executescript(sql + ";")
            else:
                state.db.execute(sql)
        assert (Path(state._directory.name) / "observed.sqlite").stat().st_size <= state.spool_bytes
        with pytest.raises(StateMismatch, match="verification_spool_capacity_exceeded"):
            state.db.commit()


def test_excess_protected_history_fails_before_inserting_or_consuming_more_rows(rows):
    extra = operation(99, 300, 4, "issue.update", {"title": "Another update", "state": "closed"})

    def observed():
        yield from rows
        yield extra
        pytest.fail("The protected row count must stop this excessive stream")

    with ProtectedState(receipt_cut(rows)) as state:
        with pytest.raises(StateMismatch, match="accepted_history_mismatch"):
            state.load_journal(observed())
        assert state.db.execute("SELECT COUNT(*) FROM journal").fetchone()[0] == len(rows)
        assert state.db.execute("SELECT 1 FROM journal WHERE event_id=?", (extra["event_id"],)).fetchone() is None
        assert not state.history_anchored


def test_excess_protected_effect_fails_before_inserting_or_consuming_more_rows(rows):
    effects, cut = effects_for(rows)
    source = operation_from_row(rows[2])
    extra = {
        "effect_id": effect_identity(source.event_id, "delivery", uid(901)),
        "event_id": source.event_id,
        "effect_kind": "delivery",
        "destination": uid(901),
        "payload": {"operation": source.request(), "url": "http://customer-webhook/deliveries"},
        "state": "done",
        "attempts": 1,
    }

    def observed():
        yield from effects
        yield extra
        pytest.fail("The exact private effect count must stop this excessive stream")

    with ProtectedState(receipt_cut(rows)) as state:
        state.load_journal(rows)
        with pytest.raises(StateMismatch, match="business_effect_history_mismatch"):
            state.load_effects(observed(), cut)
        assert state.db.execute("SELECT COUNT(*) FROM effects").fetchone()[0] == cut.count
        assert state.db.execute("SELECT 1 FROM effects WHERE effect_id=?", (extra["effect_id"],)).fetchone() is None


@pytest.mark.parametrize("spool_bytes", [True, 0, 65535, 8 * 1024**3 + 1, 65536.0, None])
def test_invalid_trusted_budget_fails_before_allocating_scratch(rows, monkeypatch, spool_bytes):
    def forbidden(**_kwargs):
        pytest.fail("Invalid budgets must not allocate verifier scratch")

    monkeypatch.setattr(codehub_state, "TemporaryDirectory", forbidden)
    with pytest.raises(ValueError, match="trusted spool budget"):
        ProtectedState(receipt_cut(rows), spool_bytes=spool_bytes)


def test_constructor_allocation_failure_removes_its_private_directory(rows, tmp_path, monkeypatch):
    def fail_connect(*_args, **_kwargs):
        raise OSError("Scratch device unavailable")

    monkeypatch.setattr(codehub_state.sqlite3, "connect", fail_connect)
    with pytest.raises(OSError, match="Scratch device unavailable"):
        ProtectedState(receipt_cut(rows), scratch_dir=tmp_path)
    assert not tuple(tmp_path.iterdir())


def test_invalid_scratch_directory_does_not_fall_back_to_tmp(rows, tmp_path):
    for path in (Path("relative-scratch"), tmp_path / "missing"):
        with pytest.raises(ValueError, match="absolute trusted directory"):
            ProtectedState(receipt_cut(rows), scratch_dir=path)
