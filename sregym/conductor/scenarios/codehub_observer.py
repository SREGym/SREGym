"""Independent business-delivery receipts; expectations and verdicts are never served."""

import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import sqlite3
import threading
import time
from contextlib import suppress
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer as BaseThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

from sregym.generators.workload.codehub import canonical


class ObserverCapacityError(RuntimeError):
    pass


class ThreadingHTTPServer(BaseThreadingHTTPServer):
    """Finite incoming request concurrency, including incomplete request bodies."""

    def __init__(self, address, handler, *, maximum_requests=32):
        self._slots = threading.BoundedSemaphore(maximum_requests)
        self._active = threading.Condition()
        self._requests = set()
        super().__init__(address, handler)

    def process_request(self, request, address):
        if not self._slots.acquire(blocking=False):
            try:
                request.settimeout(1)
                request.sendall(b"HTTP/1.0 503 Service Unavailable\r\nContent-Length: 0\r\n\r\n")
            except OSError:
                pass
            finally:
                self.shutdown_request(request)
            return
        with self._active:
            self._requests.add(request)
        try:
            super().process_request(request, address)
        except BaseException:
            with self._active:
                self._requests.discard(request)
                self._active.notify_all()
            self._slots.release()
            raise

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            with self._active:
                self._requests.discard(request)
                self._active.notify_all()
            self._slots.release()

    def drain(self, deadline):
        with self._active:
            for request in tuple(self._requests):
                with suppress(OSError):
                    request.shutdown(2)
            while self._requests:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError("Delivery handlers did not drain within their deadline")
                self._active.wait(remaining)


class DeliveryObserver:
    """Public append-only delivery endpoint and separately authenticated local reader.

    The private listener is host-loopback only and must be qualified against the
    rootless deployment. It provides actual receiver facts, never expected data.
    """

    def __init__(
        self,
        path: Path,
        *,
        delivery_address: str,
        delivery_port: int = 0,
        byte_budget=256 * 1024**2,
        maximum_requests=32,
    ):
        if type(byte_budget) is not int or not 4 * 1024**2 <= byte_budget <= 8 * 1024**3:
            raise ValueError("Delivery receipt storage needs a bounded 4 MiB to 8 GiB budget")
        if type(maximum_requests) is not int or not 1 <= maximum_requests <= 64:
            raise ValueError("Delivery concurrency must be bounded to 1..64 requests")
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = path
        self._lock = threading.RLock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        os.chmod(path, 0o600)
        self._db.executescript(
            "PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL; PRAGMA wal_autocheckpoint=64;"
            "CREATE TABLE IF NOT EXISTS receipts(effect_id TEXT PRIMARY KEY,event_id TEXT NOT NULL,"
            "body TEXT NOT NULL,sha256 TEXT NOT NULL,attempts INTEGER NOT NULL DEFAULT 1);"
        )
        page_size = self._db.execute("PRAGMA page_size").fetchone()[0]
        pages = (byte_budget - 2 * 1024**2) // page_size
        if self._db.execute(f"PRAGMA max_page_count={pages}").fetchone()[0] != pages:
            self._db.close()
            raise ObserverCapacityError("Retained receiver data exceeds its configured storage budget")
        self.byte_budget, self.capacity_error = byte_budget, None
        self.read_token = secrets.token_urlsafe(32)
        self._public = None
        try:
            self._public = ThreadingHTTPServer(
                (delivery_address, delivery_port), self._handler(private=False), maximum_requests=maximum_requests
            )
            self._private = ThreadingHTTPServer(("127.0.0.1", 0), self._handler(private=True), maximum_requests=8)
        except BaseException:
            if self._public is not None:
                self._public.server_close()
            self._db.close()
            raise
        self.delivery_url = f"http://{delivery_address}:{self._public.server_port}/deliveries"
        self.read_url = f"http://127.0.0.1:{self._private.server_port}/receipts"
        self._threads = ()
        self._closed = False

    def append(self, body: dict, identity: str) -> dict:
        if type(body) is not dict or set(body) != {"effect_id", "event_id", "operation"}:
            raise ValueError("Delivery must have a valid envelope")
        if type(identity) is not str or not re.fullmatch(r"[0-9a-f]{64}", identity) or body["effect_id"] != identity:
            raise ValueError("Delivery identity is invalid")
        event_id = body["event_id"]
        if type(event_id) is not str or str(UUID(event_id)) != event_id or type(body["operation"]) is not dict:
            raise ValueError("Delivery event and operation are invalid")
        if body["operation"].get("event_id") != event_id:
            raise ValueError("Delivery operation has a different event identity")
        encoded = canonical(body)
        if len(encoded.encode()) > 256 * 1024:
            raise ValueError("Delivery exceeds capacity")
        checksum = hashlib.sha256(encoded.encode()).hexdigest()
        try:
            with self._lock, self._db:
                files = (self.path, Path(str(self.path) + "-wal"), Path(str(self.path) + "-shm"))
                if sum(path.stat().st_size for path in files if path.exists()) > self.byte_budget or (
                    shutil.disk_usage(self.path.parent).free < 256 * 1024**2
                ):
                    raise ObserverCapacityError("Trusted receiver storage exhausted its configured reserve")
                previous = self._db.execute("SELECT sha256 FROM receipts WHERE effect_id=?", (identity,)).fetchone()
                if previous is not None and previous[0] != checksum:
                    raise ValueError("Delivery identity already describes different content")
                if previous is None:
                    self._db.execute(
                        "INSERT INTO receipts(effect_id,event_id,body,sha256) VALUES (?,?,?,?)",
                        (identity, event_id, encoded, checksum),
                    )
                else:
                    self._db.execute("UPDATE receipts SET attempts=attempts+1 WHERE effect_id=?", (identity,))
        except (ObserverCapacityError, sqlite3.OperationalError) as error:
            self.capacity_error = type(error).__name__
            raise ObserverCapacityError("Trusted receiver storage is unavailable") from error
        return {"delivery_id": identity, "accepted": True}

    def observations(self, identities: tuple[str, ...]) -> tuple[dict, ...]:
        if (
            type(identities) is not tuple
            or len(identities) > 100
            or any(type(value) is not str or not re.fullmatch(r"[0-9a-f]{64}", value) for value in identities)
        ):
            raise ValueError("Observation identities must be a bounded batch")
        with self._lock:
            rows = []
            for identity in identities:
                value = self._db.execute("SELECT body,attempts FROM receipts WHERE effect_id=?", (identity,)).fetchone()
                if value is not None:
                    rows.append(json.loads(value[0]) | {"application_count": 1, "attempt_count": value[1]})
            return tuple(rows)

    def _handler(self, *, private: bool):
        observer = self

        class Handler(BaseHTTPRequestHandler):
            def setup(self):
                super().setup()
                self.connection.settimeout(5)
                self.expiry = threading.Timer(10, self.expire)
                self.expiry.daemon = True
                self.expiry.start()

            def expire(self):
                with suppress(OSError):
                    self.connection.shutdown(2)

            def finish(self):
                self.expiry.cancel()
                self.expiry.join(timeout=1)
                super().finish()

            def log_message(self, *_args):
                pass  # Receiver evidence is stored privately, without HTTP credential logging.

            def reply(self, status, body):
                data = canonical(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                try:
                    self.end_headers()
                    self.wfile.write(data)
                except OSError:
                    pass

            def do_POST(self):
                if private or self.path != "/deliveries":
                    self.reply(404, {"detail": "Not found"})
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= 256 * 1024 or self.headers.get("Content-Type") != "application/json":
                        raise ValueError("Invalid delivery request")
                    deadline, data = time.monotonic() + 5, bytearray()
                    while len(data) < length:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise TimeoutError("Delivery body deadline exceeded")
                        self.connection.settimeout(remaining)
                        block = self.rfile.read1(min(65536, length - len(data)))
                        if not block:
                            raise ValueError("Truncated delivery body")
                        data.extend(block)
                    body = json.loads(data)
                    result = observer.append(body, self.headers.get("Idempotency-Key", ""))
                except ObserverCapacityError:
                    self.reply(503, {"detail": "Service unavailable"})
                    return
                except TimeoutError:
                    self.reply(408, {"detail": "Request timeout"})
                    return
                except (ValueError, TypeError, UnicodeError):
                    self.reply(409, {"detail": "Invalid or conflicting delivery"})
                    return
                self.reply(200, result)

            def do_GET(self):
                if not private or urlsplit(self.path).path != "/receipts":
                    self.reply(404, {"detail": "Not found"})
                    return
                if not hmac.compare_digest(self.headers.get("Authorization", ""), f"Bearer {observer.read_token}"):
                    self.reply(403, {"detail": "Access denied"})
                    return
                try:
                    if len(self.path) > 10_000:
                        raise ValueError("Observation request exceeds capacity")
                    params = parse_qs(urlsplit(self.path).query, strict_parsing=True)
                    if set(params) != {"id"}:
                        raise ValueError("Observation request requires identities")
                    rows = observer.observations(tuple(params["id"]))
                except ValueError:
                    self.reply(400, {"detail": "Invalid observation request"})
                    return
                self.reply(200, {"receipts": rows})

        return Handler

    def start(self):
        if self._closed or self._threads:
            raise RuntimeError("Observer cannot be started twice or after closure")
        try:
            for server in (self._public, self._private):
                thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
                thread.start()
                self._threads += (thread,)
        except BaseException:
            self.close()
            raise

    def close(self):
        if self._closed:
            return
        deadline = time.monotonic() + 15
        if self._threads:
            for server in (self._public, self._private)[: len(self._threads)]:
                server.shutdown()
            for thread in self._threads:
                thread.join(timeout=max(0, deadline - time.monotonic()))
            if any(thread.is_alive() for thread in self._threads):
                raise RuntimeError("Delivery receiver did not stop within its deadline")
        self._public.server_close()
        self._private.server_close()
        self._public.drain(deadline)
        self._private.drain(deadline)
        if not self._lock.acquire(timeout=max(0, deadline - time.monotonic())):
            raise RuntimeError("Delivery evidence writer did not drain within its deadline")
        try:
            self._db.close()
            self._closed = True
        finally:
            self._lock.release()
