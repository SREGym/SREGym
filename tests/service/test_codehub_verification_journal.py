import hashlib
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest

from sregym.conductor.scenarios.codehub_observer import DeliveryObserver
from sregym.generators.workload.codehub import Operation, ReceiptLedger, canonical
from sregym.service.codehub_verification_journal import CodeHubVerificationJournal
from sregym.service.verifier_runtime import VerifierError, _resource_call
from sregym.service.verifier_state import restore_oracle, snapshot_oracle
from sregym.service.verifier_worker import RemoteWorkload


@pytest.fixture
def state(tmp_path):
    ledger = ReceiptLedger(tmp_path / "owner-only" / "private.sqlite")
    operation = Operation(
        str(uuid4()),
        str(uuid4()),
        str(uuid4()),
        str(uuid4()),
        1,
        "issue.create",
        canonical({"title": "Handle request cancellation", "body": "Release outstanding resources."}),
        str(uuid4()),
    )
    journal = CodeHubVerificationJournal(ledger, {operation.tenant_id: "group-0"})
    yield ledger, operation, journal
    ledger.close()


def effects(operation):
    return [
        {
            "effect_id": hashlib.sha256(f"{operation.event_id}/search/{operation.entity_id}".encode()).hexdigest(),
            "event_id": operation.event_id,
            "effect_kind": "search",
            "destination": operation.entity_id,
            "payload": operation.request(),
        }
    ]


def receipt(operation):
    return operation.observed_row() | {
        "record_key": operation.record_key,
        "region": "region-a",
        "replayed": False,
        "operation_sha256": hashlib.sha256(canonical(operation.observed_row()).encode()).hexdigest(),
    }


def test_parent_receipt_rpc_preserves_closed_provenance_and_rejects_open_history(state, tmp_path):
    ledger, original, journal = state
    project = replace(
        original,
        entity_id=original.project_id,
        kind="project.create",
        payload_json=canonical({"slug": "router", "name": "Router", "default_ref": "refs/heads/main"}),
    )
    epoch = journal.begin_epoch()
    journal.request(epoch, "group-0", project.observed_row(), effects(project))
    journal.acknowledge(epoch, project.event_id, "http://api", 1, 201, receipt(project))
    with pytest.raises(ValueError, match="absent"):
        journal.project_receipts([[project.tenant_id, project.project_id]])
    journal.close_epoch(epoch)
    payload, resources = snapshot_oracle(SimpleNamespace(journal=journal), tmp_path)
    restored = restore_oracle(
        payload,
        lambda index: RemoteWorkload(
            index, lambda i, op, args: _resource_call(resources, {"index": i, "op": op, "args": args})
        ),
    ).journal
    assert restored.project_receipts([[project.tenant_id, project.project_id]]) == [
        {
            "group": "group-0",
            "operations": [project.observed_row()],
            "effects": effects(project),
            "epochs": [epoch],
        }
    ]
    for invalid in (
        [[project.tenant_id]],
        [[project.tenant_id, "bad-id"]],
        [[project.tenant_id, project.project_id]] * 2,
    ):
        with pytest.raises(ValueError):
            journal.project_receipts(invalid)


def test_private_handle_roundtrip_keeps_database_and_path_outside_snapshot(state, tmp_path):
    ledger, operation, journal = state
    payload, resources = snapshot_oracle(SimpleNamespace(journal=journal), tmp_path)
    assert resources == [journal]
    assert b"private.sqlite" not in payload
    restored = restore_oracle(
        payload,
        lambda index: RemoteWorkload(
            index, lambda i, op, args: _resource_call(resources, {"index": i, "op": op, "args": args})
        ),
    ).journal
    epoch = restored.begin_epoch()
    assert restored.request(epoch, "group-0", operation.observed_row(), effects(operation)) is True
    assert restored.acknowledge(
        epoch, operation.event_id, "http://ordinary-api/v1/operations", 2.5, 201, receipt(operation)
    )
    assert restored.close_epoch(epoch)
    assert ledger.cut().operations == 1
    assert ledger.expected_effects(journal.tenant_groups)["group-0"] == tuple(effects(operation))
    with pytest.raises(VerifierError, match="receipt operation"):
        _resource_call(resources, {"index": 0, "op": "stop", "args": []})
    with pytest.raises(VerifierError, match="receipt operation"):
        _resource_call(resources, {"index": 0, "op": "journal_request", "args": []})


def test_partial_attempt_receipts_survive_and_pending_cannot_close(state):
    ledger, operation, journal = state
    epoch = journal.begin_epoch()
    journal.request(epoch, "group-0", operation.observed_row(), effects(operation))
    with pytest.raises(RuntimeError, match="unresolved"):
        journal.close_epoch(epoch)
    # The response may be retried with the identical request after it was lost.
    journal.request(epoch, "group-0", operation.observed_row(), effects(operation))
    journal.acknowledge(epoch, operation.event_id, "https://ordinary-api/v1/operations", 8, 200, receipt(operation))
    rejected = replace(operation, event_id=str(uuid4()), entity_id=str(uuid4()))
    journal.request(epoch, "group-0", rejected.observed_row(), effects(rejected))
    journal.reject(epoch, rejected.event_id, "https://ordinary-api/v1/operations", 3, 403)
    journal.close_epoch(epoch)
    assert ledger.cut().operations == 1
    assert len(ledger.expected_effects(journal.tenant_groups)["group-0"]) == 1


def test_wrong_group_epoch_receipt_and_changed_expectations_fail_closed(state):
    ledger, operation, journal = state
    epoch = journal.begin_epoch()
    with pytest.raises(ValueError, match="frozen tenant"):
        journal.request(epoch, "group-1", operation.observed_row(), effects(operation))
    with pytest.raises(ValueError, match="owner-allocated"):
        journal.request(epoch + 1, "group-0", operation.observed_row(), effects(operation))
    journal.request(epoch, "group-0", operation.observed_row(), effects(operation))
    with pytest.raises(ValueError, match="expectations cannot change"):
        journal.request(epoch, "group-0", operation.observed_row(), [])
    with pytest.raises(ValueError, match="full requested"):
        journal.acknowledge(
            epoch, operation.event_id, "http://api", 3, 201, receipt(operation) | {"actor_id": str(uuid4())}
        )
    assert len(ledger.unresolved()) == 1


def test_fresh_git_provenance_is_recorded_before_io_and_is_immutable(state):
    ledger, operation, journal = state
    operation = replace(
        operation,
        kind="repository.push",
        payload_json=canonical(
            {
                "ref": "refs/heads/customer-change",
                "commit_sha": "a" * 40,
            }
        ),
    )
    provenance = {
        "project_id": operation.project_id,
        "refs": [{"ref": "refs/heads/customer-change", "commit_sha": "a" * 40}],
        "files": [{"commit_sha": "a" * 40, "path": "src/settings.py", "sha256": "b" * 64}],
        "bundles": [{"commit_sha": "a" * 40, "sha256": "c" * 64}],
    }
    epoch = journal.begin_epoch()
    journal.request(epoch, "group-0", operation.observed_row(), effects(operation), provenance)
    assert ledger.git_provenance() == ()  # Pending Git IO is never called a committed outcome.
    with pytest.raises(RuntimeError, match="unresolved"):
        journal.close_epoch(epoch)
    with pytest.raises(ValueError, match="expectations cannot change"):
        journal.request(
            epoch,
            "group-0",
            operation.observed_row(),
            effects(operation),
            provenance
            | {
                "bundles": [{"commit_sha": "a" * 40, "sha256": "d" * 64}],
            },
        )
    journal.acknowledge(epoch, operation.event_id, "http://api", 3, 201, receipt(operation))
    journal.close_epoch(epoch)
    assert ledger.git_provenance() == (provenance,)


def test_receiver_pipe_transports_only_actual_facts_not_listener_or_database(state, tmp_path):
    ledger, operation, journal = state
    observer = DeliveryObserver(tmp_path / "private-receiver.sqlite", delivery_address="127.0.0.1")
    identity = hashlib.sha256(b"ordinary-hook").hexdigest()
    try:
        observer.append(
            {"effect_id": identity, "event_id": operation.event_id, "operation": operation.request()}, identity
        )
        journal.observer = observer
        payload, resources = snapshot_oracle(SimpleNamespace(journal=journal), tmp_path)
        assert observer.read_token.encode() not in payload
        assert b"private-receiver.sqlite" not in payload
        restored = restore_oracle(
            payload,
            lambda index: RemoteWorkload(
                index, lambda i, op, args: _resource_call(resources, {"index": i, "op": op, "args": args})
            ),
        ).journal
        facts = restored.delivery_receipts([identity])
        assert set(facts) == {"receipts"}
        assert facts["receipts"][0]["operation"] == operation.request()
        assert facts["receipts"][0]["application_count"] == 1
        with pytest.raises(ValueError, match="bounded"):
            restored.delivery_receipts([identity] * 101)
    finally:
        observer.close()


def test_continuing_progress_only_returns_closed_actual_acknowledgments_over_private_handle(state, tmp_path):
    ledger, first, journal = state
    final = replace(first, event_id=str(uuid4()), client_revision=2, kind="issue.update")
    epoch = ledger.begin_epoch()
    for operation in (first, final):
        ledger.request(operation, epoch, effects=effects(operation))
        ledger.acknowledge(operation.event_id, "http://api", 1, 201)
    journal.traffic_source = lambda: {
        "running": True,
        "groups": {"group-0": {"age_seconds": 0.1, "journeys": [[1, epoch, [first.event_id, final.event_id]]]}},
    }
    assert journal.traffic_progress()["groups"] == []
    ledger.close_epoch(epoch)
    payload, resources = snapshot_oracle(SimpleNamespace(journal=journal), tmp_path)
    assert first.event_id.encode() not in payload and b"traffic_source" not in payload
    restored = restore_oracle(
        payload,
        lambda index: RemoteWorkload(
            index, lambda i, op, args: _resource_call(resources, {"index": i, "op": op, "args": args})
        ),
    ).journal
    observed = restored.traffic_progress()
    assert observed["running"] and observed["groups"][0]["operations"] == [first.observed_row(), final.observed_row()]
    assert observed["groups"][0]["effects"] == effects(first) + effects(final)


def test_receiver_capacity_failure_after_snapshot_remains_harness_failure(state, tmp_path):
    _ledger, _operation, journal = state
    observer = DeliveryObserver(tmp_path / "receiver.sqlite", delivery_address="127.0.0.1")
    journal.observer = observer
    try:
        payload, resources = snapshot_oracle(SimpleNamespace(journal=journal), tmp_path)
        restored = restore_oracle(
            payload,
            lambda index: RemoteWorkload(
                index, lambda i, op, args: _resource_call(resources, {"index": i, "op": op, "args": args})
            ),
        ).journal
        observer.capacity_error = "storage full"
        with pytest.raises((RuntimeError, VerifierError), match="capacity"):
            restored.delivery_receipts(["a" * 64])
    finally:
        observer.close()
