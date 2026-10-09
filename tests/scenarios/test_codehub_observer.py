import hashlib
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

    def bind(address, handler):
        if opened:
            raise OSError("Private listener binding failed")
        server = original(address, handler)
        opened.append(server)
        return server

    monkeypatch.setattr(codehub_observer, "ThreadingHTTPServer", bind)
    with pytest.raises(OSError, match="binding failed"):
        DeliveryObserver(tmp_path / "private.sqlite", delivery_address="127.0.0.1")
    assert opened[0].socket.fileno() == -1
