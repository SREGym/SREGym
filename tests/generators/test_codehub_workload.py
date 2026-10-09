from dataclasses import replace
from uuid import uuid4

import httpx
import pytest

from sregym.generators.workload.codehub import Operation, ReceiptLedger, WorkloadClient, canonical


@pytest.fixture
def operation():
    return Operation(
        str(uuid4()),
        str(uuid4()),
        str(uuid4()),
        str(uuid4()),
        1,
        "issue.create",
        canonical({"title": "Improve request retries", "body": "Use bounded exponential backoff."}),
        str(uuid4()),
    )


@pytest.fixture
def ledger(tmp_path):
    value = ReceiptLedger(tmp_path / "private" / "receipts.sqlite")
    yield value
    value.close()


def test_timeout_and_503_do_not_become_acknowledgments(ledger, operation):
    def handler(request):
        if request.headers.get("authorization") != "Bearer normal-user-token":
            raise AssertionError("Missing ordinary authentication")
        return httpx.Response(503)

    client = WorkloadClient("http://api.example", "normal-user-token", ledger, transport=httpx.MockTransport(handler))
    try:
        assert not client.submit(operation, epoch=0, attempts=2, retry_delay=0)
        assert len(ledger.unresolved()) == 1
        with pytest.raises(RuntimeError, match="unresolved"):
            ledger.close_epoch(0)
        assert ledger.cut().operations == 0
    finally:
        client.close()


def test_retry_after_lost_response_keeps_the_exact_identity(ledger, operation):
    requests = []

    def handler(request):
        requests.append(request.content)
        if len(requests) == 1:
            raise httpx.ReadTimeout("Response was lost")
        return httpx.Response(200, json=operation.observed_row())

    client = WorkloadClient("http://api.example", "normal-user-token", ledger, transport=httpx.MockTransport(handler))
    try:
        assert client.submit(operation, epoch=0, retry_delay=0)
        assert requests[0] == requests[1]
        ledger.close_epoch(0)
        assert ledger.cut().operations == 1
    finally:
        client.close()


def test_closed_cut_preserves_both_revisions_during_fresh_traffic(ledger, operation):
    second = replace(
        operation, event_id=str(uuid4()), client_revision=2, payload_json=canonical({"title": "Use jitter"})
    )
    for event in (operation, second):
        ledger.request(event, 0)
        ledger.acknowledge(event.event_id, "http://api.example", 12, 201)
    ledger.close_epoch(0)
    cut = ledger.cut()
    assert cut.operations == 2
    assert cut.entities == (operation.record_key,)
    fresh = replace(operation, event_id=str(uuid4()), entity_id=str(uuid4()))
    ledger.request(fresh, 1)
    ledger.acknowledge(fresh.event_id, "http://api.example", 12, 201)
    assert ledger.cut() == cut
    with pytest.raises(ValueError, match="disjoint"):
        ledger.request(replace(second, event_id=str(uuid4()), client_revision=3), 1)


def test_conflicting_retry_and_revision_are_rejected(ledger, operation):
    ledger.request(operation, 0)
    with pytest.raises(ValueError, match="identity"):
        ledger.request(replace(operation, payload_json=canonical({"title": "Other text"})), 0)
    with pytest.raises(Exception, match="UNIQUE"):
        ledger.request(replace(operation, event_id=str(uuid4())), 0)


def test_same_entity_identity_in_distinct_tenants_is_independent(ledger, operation):
    other = replace(operation, event_id=str(uuid4()), tenant_id=str(uuid4()))
    for event in (operation, other):
        ledger.request(event, 0)
        ledger.acknowledge(event.event_id, "http://api.example", 12, 201)
    ledger.close_epoch(0)
    assert ledger.cut().operations == 2
    assert len(ledger.cut().entities) == 2
    cuts = ledger.partition_cuts({operation.tenant_id: "group-a", other.tenant_id: "group-b"})
    assert cuts["group-a"].entities == (operation.record_key,)
    assert cuts["group-b"].entities == (other.record_key,)
    assert cuts["group-a"].journal_sha256 != cuts["group-b"].journal_sha256
    with pytest.raises(ValueError, match="omits"):
        ledger.partition_cuts({operation.tenant_id: "group-a"})


def test_receipts_survive_reopening_and_definitive_rejection_is_not_a_write(tmp_path, operation):
    path = tmp_path / "receipts.sqlite"
    ledger = ReceiptLedger(path)
    ledger.request(operation, 0)
    ledger.reject(operation.event_id, "http://api.example", 12, 403)
    ledger.close_epoch(0)
    cut = ledger.cut()
    ledger.close()
    reopened = ReceiptLedger(path)
    try:
        assert reopened.cut() == cut
        assert cut.operations == 0
    finally:
        reopened.close()


def test_server_receipt_cannot_acknowledge_a_different_operation(ledger, operation):
    client = WorkloadClient(
        "http://api.example",
        "normal-user-token",
        ledger,
        transport=httpx.MockTransport(lambda _: httpx.Response(201, json={"event_id": str(uuid4())})),
    )
    try:
        with pytest.raises(RuntimeError, match="invalid committed receipt"):
            client.submit(operation, epoch=0)
        assert len(ledger.unresolved()) == 1
    finally:
        client.close()
