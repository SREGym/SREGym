import hashlib
import socket
import threading
import time
from contextlib import suppress
from uuid import uuid4

import httpx
import pytest

from sregym.conductor.scenarios import codehub_observer
from sregym.conductor.scenarios.codehub_observer import DeliveryObserver


def test_real_delivery_deduplicates_business_effects_and_keeps_attempts_private(tmp_path):
    observer = DeliveryObserver(tmp_path / "private" / "receiver.sqlite", delivery_address="127.0.0.1")
    observer.start()
    identity = hashlib.sha256(b"normal-delivery").hexdigest()
    event = str(uuid4())
    body = {"effect_id": identity, "event_id": event, "operation": {"event_id": event, "kind": "issue.create"}}
    try:
        for _ in range(2):
            response = httpx.post(observer.delivery_url, json=body, headers={"Idempotency-Key": identity})
            assert response.status_code == 200
            assert set(response.json()) == {"delivery_id", "accepted"}
        assert httpx.get(observer.read_url, params={"id": identity}).status_code == 403
        private = httpx.get(
            observer.read_url, params={"id": identity}, headers={"Authorization": f"Bearer {observer.read_token}"}
        ).json()["receipts"]
        assert private[0]["application_count"] == 1
        assert private[0]["attempt_count"] == 2
        assert httpx.get(observer.delivery_url).status_code == 404
        changed = body | {"operation": {"event_id": event, "kind": "issue.update"}}
        assert httpx.post(observer.delivery_url, json=changed, headers={"Idempotency-Key": identity}).status_code == 409
        assert observer.observations((identity,))[0]["operation"] == body["operation"]
    finally:
        observer.close()
        observer.close()


def test_delivery_interface_cannot_modify_private_records_or_paths(tmp_path):
    observer = DeliveryObserver(tmp_path / "private" / "receiver.sqlite", delivery_address="127.0.0.1")
    try:
        with pytest.raises(ValueError):
            observer.append({"path": "../../expectations", "value": "pass"}, "0" * 64)
        with pytest.raises(ValueError):
            observer.observations(("0" * 64,) * 101)
    finally:
        observer.close()


def test_private_listener_bind_failure_closes_public_listener(tmp_path, monkeypatch):
    original = codehub_observer.ThreadingHTTPServer
    opened = []

    def bind(address, handler, **kwargs):
        if opened:
            raise OSError("Private listener binding failed")
        server = original(address, handler, **kwargs)
        opened.append(server)
        return server

    monkeypatch.setattr(codehub_observer, "ThreadingHTTPServer", bind)
    with pytest.raises(OSError, match="binding failed"):
        DeliveryObserver(tmp_path / "private.sqlite", delivery_address="127.0.0.1")
    assert opened[0].socket.fileno() == -1


def test_receiver_concurrency_rejects_excess_incomplete_requests_and_recovers(tmp_path):
    observer = DeliveryObserver(tmp_path / "receipts.sqlite", delivery_address="127.0.0.1", maximum_requests=1)
    observer.start()
    first = socket.create_connection(("127.0.0.1", observer._public.server_port), timeout=2)
    try:
        first.sendall(b"POST /deliveries HTTP/1.0\r\nContent-Length: 100\r\nContent-Type: application/json\r\n\r\n")
        deadline = time.monotonic() + 2
        while observer._public._slots._value and time.monotonic() < deadline:
            time.sleep(0.01)
        with socket.create_connection(("127.0.0.1", observer._public.server_port), timeout=2) as second:
            assert second.recv(1024).startswith(b"HTTP/1.0 503")
    finally:
        first.close()
        observer.close()


def test_receiver_storage_limit_preserves_existing_receipts(tmp_path):
    observer = DeliveryObserver(tmp_path / "receipts.sqlite", delivery_address="127.0.0.1", byte_budget=4 * 1024**2)
    retained = []
    try:
        for index in range(64):
            identity = hashlib.sha256(str(index).encode()).hexdigest()
            event = str(uuid4())
            body = {"effect_id": identity, "event_id": event, "operation": {"event_id": event, "body": "x" * 128000}}
            try:
                observer.append(body, identity)
                retained.append(identity)
            except codehub_observer.ObserverCapacityError:
                break
        assert 0 < len(retained) < 64 and observer.capacity_error is not None
        assert len(observer.observations(tuple(retained))) == len(retained)
        assert sum(path.stat().st_size for path in tmp_path.iterdir()) <= observer.byte_budget
    finally:
        observer.close()


def test_close_interrupts_incomplete_body_and_drains_handlers_before_storage_close(tmp_path):
    observer = DeliveryObserver(tmp_path / "receiver.sqlite", delivery_address="127.0.0.1")
    observer.start()
    connection = socket.create_connection(("127.0.0.1", observer._public.server_port), timeout=2)
    try:
        connection.sendall(
            b"POST /deliveries HTTP/1.0\r\nContent-Length: 100\r\nContent-Type: application/json\r\n\r\n"
        )
        deadline = time.monotonic() + 2
        while not observer._public._requests and time.monotonic() < deadline:
            time.sleep(0.01)
        assert observer._public._requests
        observer.close()
        assert observer._closed and not observer._public._requests
        assert not observer._private._requests and all(not thread.is_alive() for thread in observer._threads)
    finally:
        connection.close()
        observer.close()


def test_close_failure_keeps_storage_open_and_is_retryable(tmp_path, monkeypatch):
    observer = DeliveryObserver(tmp_path / "receiver.sqlite", delivery_address="127.0.0.1")
    drain = observer._public.drain
    monkeypatch.setattr(
        observer._public, "drain", lambda deadline: (_ for _ in ()).throw(RuntimeError("Drain deadline"))
    )
    with pytest.raises(RuntimeError, match="Drain deadline"):
        observer.close()
    assert not observer._closed and observer._db.execute("SELECT COUNT(*) FROM receipts").fetchone() == (0,)
    monkeypatch.setattr(observer._public, "drain", drain)
    observer.close()
    assert observer._closed


def test_close_drains_actual_append_and_retains_committed_effect_after_lost_response(tmp_path, monkeypatch):
    observer = DeliveryObserver(tmp_path / "receiver.sqlite", delivery_address="127.0.0.1")
    observer.start()
    entered, release, stop_done = threading.Event(), threading.Event(), threading.Event()
    identity, event = hashlib.sha256(b"append-drain").hexdigest(), str(uuid4())
    errors = []

    def blocked_insert(statement):
        if statement.startswith("INSERT INTO receipts"):
            entered.set()
            assert release.wait(3)

    observer._db.set_trace_callback(blocked_insert)

    def request():
        with suppress(httpx.HTTPError):
            httpx.post(
                observer.delivery_url,
                json={"effect_id": identity, "event_id": event, "operation": {"event_id": event}},
                headers={"Idempotency-Key": identity},
                timeout=3,
            )

    def stop():
        try:
            observer.close()
        except Exception as error:
            errors.append(error)
        finally:
            stop_done.set()

    customer, closer = threading.Thread(target=request), threading.Thread(target=stop)
    try:
        customer.start()
        assert entered.wait(2)
        closer.start()
        assert not stop_done.wait(0.1) and not observer._closed
        release.set()
        customer.join(3)
        closer.join(3)
        assert stop_done.is_set() and not errors and observer._closed
        restored = DeliveryObserver(tmp_path / "receiver.sqlite", delivery_address="127.0.0.1")
        try:
            assert restored.observations((identity,))[0]["application_count"] == 1
        finally:
            restored.close()
    finally:
        release.set()
        customer.join(3)
        if closer.ident is not None:
            closer.join(3)
        observer.close()
