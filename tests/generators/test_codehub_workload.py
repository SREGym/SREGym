import sqlite3
import threading
from dataclasses import replace
from uuid import uuid4

import httpx
import pytest

from sregym.generators.workload import codehub as module
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


def test_read_snapshot_is_read_only_and_releases_its_connection_on_failure(ledger, operation):
    ledger.request(operation, 0)
    ledger.acknowledge(operation.event_id, "http://api.example", 1, 201)
    ledger.close_epoch(0)
    with pytest.raises(RuntimeError, match="stop snapshot"), ledger.read_snapshot() as frozen:
        assert frozen.cut().operations == 1
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            frozen.begin_epoch()
        newer = replace(operation, event_id=str(uuid4()), entity_id=str(uuid4()))
        ledger.request(newer, 1)
        ledger.acknowledge(newer.event_id, "http://api.example", 1, 201)
        ledger.close_epoch(1)
        assert frozen.cut().operations == 1 and ledger.cut().operations == 2
        raise RuntimeError("stop snapshot")
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        frozen.cut()


def test_cancelled_read_snapshot_interrupts_large_query_and_keeps_live_ledger_writable(ledger):
    cancel = threading.Event()
    with pytest.raises(TimeoutError, match="cancellation"), ledger.read_snapshot(cancel=cancel) as frozen:
        cancel.set()
        frozen._db.execute(
            "WITH RECURSIVE rows(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM rows WHERE x<100000) SELECT SUM(x) FROM rows"
        ).fetchone()
    assert ledger.begin_epoch() == 0


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


def test_definitive_rejection_retains_bounded_detail_without_accepting_a_write(ledger, operation):
    client = WorkloadClient(
        "http://api.example",
        "normal-user-token",
        ledger,
        transport=httpx.MockTransport(lambda _: httpx.Response(422, json={"detail": "x" * 800})),
    )
    try:
        assert not client.submit(operation, epoch=0)
        assert client.last_rejection == (422, "x" * 512)
        ledger.close_epoch(0)
        assert ledger.cut().operations == 0
    finally:
        client.close()


def test_oversized_streamed_receipt_cannot_accept_or_exhaust_owner_memory(ledger, operation):
    class Oversized(httpx.SyncByteStream):
        def __iter__(self):
            for _ in range(5):
                yield b"x" * 65536

    client = WorkloadClient(
        "http://api.example",
        "normal-user-token",
        ledger,
        transport=httpx.MockTransport(lambda _: httpx.Response(201, stream=Oversized())),
    )
    try:
        with pytest.raises(RuntimeError, match="bounded response capacity"):
            client.submit(operation, epoch=0, attempts=1)
        assert len(ledger.unresolved()) == 1 and ledger.cut().operations == 0
    finally:
        client.close()


def test_slow_dribble_receipt_has_absolute_deadline_and_remains_pending(ledger, operation, monkeypatch):
    clock = [0.0]

    class Dribble(httpx.SyncByteStream):
        def __iter__(self):
            for _ in range(20):
                clock[0] += 1
                yield b" "

    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    client = WorkloadClient(
        "http://api.example",
        "normal-user-token",
        ledger,
        transport=httpx.MockTransport(lambda _: httpx.Response(201, stream=Dribble())),
    )
    try:
        assert not client.submit(operation, epoch=0, attempts=1)
        assert clock[0] == 10 and len(ledger.unresolved()) == 1
    finally:
        client.close()


@pytest.mark.parametrize("mode", ["headers", "body", "cancel"])
def test_real_slow_response_obeys_absolute_deadline_and_keeps_receipt_pending(ledger, operation, mode):
    import socket
    import time

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listener.settimeout(2)
    stop, cancelled = threading.Event(), threading.Event()
    errors = []

    def serve():
        try:
            with listener.accept()[0] as connection:
                connection.settimeout(1)
                connection.recv(65536)
                if mode == "body":
                    connection.sendall(b"HTTP/1.0 201 Created\r\nContent-Length: 500\r\n\r\n")
                    payload = b" " * 500
                else:
                    payload = b"HTTP/1.0 201 Created\r\nX-Slow: " + b"a" * 500
                for byte in payload:
                    if stop.wait(0.04):
                        return
                    connection.sendall(bytes([byte]))
        except OSError as error:
            errors.append(type(error).__name__)

    thread = threading.Thread(target=serve, name="slow-receipt-server")
    thread.start()
    client = WorkloadClient(f"http://127.0.0.1:{listener.getsockname()[1]}", "normal-token", ledger, cancel=cancelled)
    client.deadline = time.monotonic() + (5 if mode == "cancel" else 0.4)
    timer = threading.Timer(0.2, cancelled.set) if mode == "cancel" else None
    if timer:
        timer.start()
    started = time.monotonic()
    try:
        assert client.submit(operation, epoch=ledger.begin_epoch(), attempts=1) is False
        assert time.monotonic() - started < 1.5
        assert ledger.unresolved()[0]["event_id"] == operation.event_id
        assert ledger.cut().operations == 0
    finally:
        stop.set()
        if timer:
            timer.cancel()
            timer.join(timeout=1)
        client.close()
        listener.close()
        thread.join(timeout=2)
    assert not thread.is_alive()


def test_receipt_capacity_refuses_growth_without_losing_prior_closed_acknowledgments(tmp_path):
    from uuid import uuid4

    ledger = ReceiptLedger(tmp_path / "private.sqlite", byte_budget=4 * 1024**2)
    initial = ledger.begin_epoch()
    operation = Operation(
        str(uuid4()),
        str(uuid4()),
        str(uuid4()),
        str(uuid4()),
        1,
        "issue.create",
        canonical({"title": "Retry review", "state": "open"}),
        str(uuid4()),
    )
    ledger.request(operation, initial)
    ledger.acknowledge(operation.event_id, "http://api", 1, 201)
    ledger.close_epoch(initial)
    baseline = ledger.cut()
    epoch = ledger.begin_epoch()
    errors = []
    try:
        for _ in range(64):
            large = replace(
                operation,
                event_id=str(uuid4()),
                entity_id=str(uuid4()),
                payload_json=canonical({"title": "Review", "body": "x" * 128000}),
            )
            try:
                ledger.request(large, epoch)
            except (RuntimeError, sqlite3.OperationalError) as error:
                errors.append(error)
                break
        assert errors and ledger.cut() == baseline
        assert sum(path.stat().st_size for path in tmp_path.iterdir()) <= 4 * 1024**2
    finally:
        ledger.close()
