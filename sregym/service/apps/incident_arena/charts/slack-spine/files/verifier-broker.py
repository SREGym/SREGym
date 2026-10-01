"""Minimal fixed-target Kubernetes restart broker for generated v2 tasks.

The pod receives an exact-name get/delete Role.  It cannot list, watch, patch,
or choose a target at request time.  The only mutating endpoint is protected by
the existing grader capability and writes an idempotent verifier receipt.
"""

from __future__ import annotations

import http.server
import json
import os
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from .textual import find_image_digest, pinned_image_digest

HEADER = "X-SRE-World-Grader-Access"
SA_ROOT = Path("/var/run/secrets/kubernetes.io/serviceaccount")
RECEIPT = Path("/var/run/verifier/receipt.json")


class _PodIdentityPending(RuntimeError):
    """The exact pod exists, but Kubernetes has not materialized its status yet."""


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"verifier broker: required environment variable is missing: {name}")
    return value


def _json_bytes(payload: Any) -> bytes:
    return (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _persist_state(payload: dict[str, Any]) -> None:
    """Atomically persist and fsync the restart journal on the broker PVC."""

    RECEIPT.parent.mkdir(parents=True, exist_ok=True)
    tmp = RECEIPT.with_suffix(".tmp")
    with tmp.open("wb") as stream:
        stream.write(_json_bytes(payload))
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(tmp, 0o600)
    os.replace(tmp, RECEIPT)
    directory_fd = os.open(RECEIPT.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


class Broker:
    def __init__(self) -> None:
        self.target_pod = _required_env("TARGET_POD")
        self.target_service = _required_env("TARGET_SERVICE")
        self.readiness_path = _required_env("READINESS_PATH")
        self.channels = [item for item in _required_env("CHALLENGE_CHANNELS").split(",") if item]
        self.timeout_s = int(_required_env("CHALLENGE_TIMEOUT_S"))
        if not 30 <= self.timeout_s <= 600 or not self.channels:
            raise RuntimeError("verifier broker: timeout/channels configuration is invalid")
        token_path = Path(_required_env("GRADER_ACCESS_TOKEN_FILE"))
        self.grader_token = token_path.read_text().strip()
        if len(self.grader_token) < 32:
            raise RuntimeError("verifier broker: grader capability is missing or too short")
        self.namespace = (SA_ROOT / "namespace").read_text().strip()
        self.kube_token = (SA_ROOT / "token").read_text().strip()
        if not self.namespace or not self.kube_token:
            raise RuntimeError("verifier broker: Kubernetes service-account identity is unavailable")
        host = _required_env("KUBERNETES_SERVICE_HOST")
        port = _required_env("KUBERNETES_SERVICE_PORT_HTTPS")
        self.kube_base = f"https://{host}:{port}"
        self.ssl_context = ssl.create_default_context(cafile=str(SA_ROOT / "ca.crt"))
        RECEIPT.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()

    def _kube(self, method: str) -> tuple[int, Any]:
        quoted_ns = urllib.parse.quote(self.namespace, safe="")
        quoted_pod = urllib.parse.quote(self.target_pod, safe="")
        url = f"{self.kube_base}/api/v1/namespaces/{quoted_ns}/pods/{quoted_pod}"
        body = None
        if method == "DELETE":
            body = _json_bytes(
                {
                    "apiVersion": "v1",
                    "kind": "DeleteOptions",
                    "gracePeriodSeconds": 0,
                    "propagationPolicy": "Background",
                }
            )
        request = urllib.request.Request(
            url,
            data=body,
            method=method,
            headers={
                "Authorization": f"Bearer {self.kube_token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, context=self.ssl_context, timeout=15) as response:
                raw = response.read()
                return response.status, json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                detail = json.loads(raw) if raw else None
            except json.JSONDecodeError:
                detail = {"error": raw.decode(errors="replace")[:500]}
            return exc.code, detail

    @staticmethod
    def _pod_ready(payload: Any) -> bool:
        if not isinstance(payload, dict):
            return False
        conditions = (payload.get("status") or {}).get("conditions") or []
        return any(
            isinstance(item, dict)
            and item.get("type") == "Ready"
            and item.get("status") == "True"
            for item in conditions
        )

    @staticmethod
    def _image_digest(spec_image: str, runtime_image_id: Any) -> tuple[str, bool]:
        """Return one canonical digest across CRI implementations.

        Some Kind/containerd versions leave ``status.imageID`` empty for a
        digest-pinned image.  The immutable spec digest remains an exact image
        identity in that case; a present runtime digest must agree with it.
        """

        if runtime_image_id is not None and not isinstance(runtime_image_id, str):
            raise RuntimeError(
                "verifier broker: app container runtime image identity is malformed"
            )
        spec_digest = pinned_image_digest(spec_image)
        runtime_digest = find_image_digest(runtime_image_id or "")
        if runtime_digest is not None:
            if spec_digest is not None and spec_digest != runtime_digest:
                raise RuntimeError(
                    "verifier broker: app container runtime image identity "
                    "does not match its pinned spec"
                )
            return f"sha256:{runtime_digest}", True
        if runtime_image_id:
            raise RuntimeError(
                "verifier broker: app container runtime image identity lacks a digest"
            )
        if spec_digest is not None:
            return f"sha256:{spec_digest}", False
        raise RuntimeError(
            "verifier broker: app container has neither a runtime digest nor "
            "a digest-pinned spec"
        )

    @staticmethod
    def _pod_identity(payload: Any) -> dict[str, Any]:
        metadata = payload.get("metadata") if isinstance(payload, dict) else None
        specs = (payload.get("spec") or {}).get("containers") if isinstance(payload, dict) else None
        statuses = (payload.get("status") or {}).get("containerStatuses") if isinstance(payload, dict) else None
        if not isinstance(metadata, dict) or not isinstance(metadata.get("uid"), str):
            raise RuntimeError("verifier broker: Kubernetes pod response lacks metadata.uid")
        if not isinstance(specs, list):
            raise RuntimeError("verifier broker: target pod container spec is malformed")
        app_spec = next(
            (item for item in specs if isinstance(item, dict) and item.get("name") == "app"),
            None,
        )
        if (
            not isinstance(app_spec, dict)
            or not isinstance(app_spec.get("image"), str)
            or not app_spec["image"].strip()
        ):
            raise RuntimeError("verifier broker: target pod lacks exact app container spec")
        if statuses is None:
            if not Broker._pod_ready(payload):
                raise _PodIdentityPending("containerStatuses is not materialized")
            raise RuntimeError(
                "verifier broker: ready target pod lacks containerStatuses"
            )
        if not isinstance(statuses, list):
            raise RuntimeError("verifier broker: target pod container status is malformed")
        app_status = next(
            (item for item in statuses if isinstance(item, dict) and item.get("name") == "app"),
            None,
        )
        if not isinstance(app_status, dict):
            if not Broker._pod_ready(payload):
                raise _PodIdentityPending("app container status is not materialized")
            raise RuntimeError(
                "verifier broker: target pod lacks exact app container image identity"
            )
        if type(app_status.get("restartCount")) is not int:
            raise RuntimeError("verifier broker: app container restart count is malformed")
        runtime_image_id = app_status.get("imageID")
        if (
            (runtime_image_id is None or runtime_image_id == "")
            and pinned_image_digest(app_spec["image"]) is None
            and not Broker._pod_ready(payload)
        ):
            raise _PodIdentityPending("app container runtime image identity is not materialized")
        image_id, runtime_image_id_present = Broker._image_digest(
            app_spec["image"], runtime_image_id
        )
        return {
            "uid": metadata["uid"],
            "restart_count": app_status["restartCount"],
            "container": {
                "name": "app",
                "spec_image": app_spec["image"],
                "image_id": image_id,
                "runtime_image_id_present": runtime_image_id_present,
            },
        }

    @staticmethod
    def _pod_state_summary(payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict):
            return {"response_type": type(payload).__name__}
        metadata = payload.get("metadata") or {}
        spec = payload.get("spec") or {}
        status = payload.get("status") or {}
        if not isinstance(metadata, dict):
            metadata = {}
        if not isinstance(spec, dict):
            spec = {}
        if not isinstance(status, dict):
            status = {}
        app_spec = next(
            (
                item
                for item in spec.get("containers") or []
                if isinstance(item, dict) and item.get("name") == "app"
            ),
            None,
        )
        app_status = next(
            (
                item
                for item in status.get("containerStatuses") or []
                if isinstance(item, dict) and item.get("name") == "app"
            ),
            None,
        )
        ready = next(
            (
                item
                for item in status.get("conditions") or []
                if isinstance(item, dict) and item.get("type") == "Ready"
            ),
            None,
        )
        return {
            "name": metadata.get("name"),
            "uid": metadata.get("uid"),
            "phase": status.get("phase"),
            "ready_condition": (
                {
                    key: ready.get(key)
                    for key in ("type", "status", "reason", "message")
                    if ready.get(key) is not None
                }
                if isinstance(ready, dict)
                else None
            ),
            "app_spec": (
                {"name": app_spec.get("name"), "image": app_spec.get("image")}
                if isinstance(app_spec, dict)
                else None
            ),
            "app_status": (
                {
                    key: app_status.get(key)
                    for key in ("name", "imageID", "restartCount", "ready", "state")
                    if app_status.get(key) is not None
                }
                if isinstance(app_status, dict)
                else None
            ),
        }

    def _wait_current_identity(
        self,
        *,
        deadline: float,
        context: str,
        allow_not_found: bool,
    ) -> dict[str, Any] | None:
        last: dict[str, Any] = {"status": None, "pod": None, "pending": None}
        while time.monotonic() < deadline:
            status, payload = self._kube("GET")
            last = {
                "status": status,
                "pod": self._pod_state_summary(payload),
                "pending": None,
            }
            if status == 404:
                if allow_not_found:
                    return None
            elif status != 200:
                raise RuntimeError(
                    f"verifier broker: fixed target {context} GET failed: HTTP {status}"
                )
            else:
                try:
                    return self._pod_identity(payload)
                except _PodIdentityPending as exc:
                    last["pending"] = str(exc)
            time.sleep(2)
        raise RuntimeError(
            "verifier broker: fixed target "
            f"{context} identity did not materialize before the challenge deadline: {last!r}"
        )

    def _service_json(self, method: str, path: str, body: Any = None) -> tuple[int, Any]:
        url = f"http://{self.target_service}:8000{path}"
        raw_body = _json_bytes(body) if body is not None else None
        request = urllib.request.Request(
            url,
            data=raw_body,
            method=method,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                raw = response.read()
                return response.status, json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                return exc.code, json.loads(raw) if raw else None
            except json.JSONDecodeError:
                return exc.code, {"error": raw.decode(errors="replace")[:500]}
        except OSError as exc:
            return 0, {"error": type(exc).__name__}

    def _wait_new_ready(self, old_uid: str, *, deadline: float | None = None) -> dict[str, Any]:
        deadline = deadline if deadline is not None else time.monotonic() + self.timeout_s
        last: Any = None
        while time.monotonic() < deadline:
            status, payload = self._kube("GET")
            last = {"status": status, "pod": self._pod_state_summary(payload), "pending": None}
            if status == 200:
                try:
                    identity = self._pod_identity(payload)
                except _PodIdentityPending as exc:
                    last["pending"] = str(exc)
                    time.sleep(2)
                    continue
                if identity["uid"] != old_uid and self._pod_ready(payload):
                    health, _ = self._service_json("GET", self.readiness_path)
                    if 200 <= health < 300:
                        return identity
            elif status != 404:
                raise RuntimeError(
                    f"verifier broker: fixed pod readiness GET failed: HTTP {status}"
                )
            time.sleep(2)
        raise RuntimeError(
            "verifier broker: fixed pod did not become newly ready before the "
            f"challenge deadline: {last!r}"
        )

    def _traffic(self, challenge_id: str) -> dict[str, Any]:
        records: list[dict[str, Any]] = []
        for index, channel in enumerate(self.channels):
            client_id = f"v2-{challenge_id[-12:]}-{index}"
            text = f"verifier restart challenge {index}"
            status, body = self._service_json(
                "POST",
                "/messages",
                {"channel_id": channel, "client_msg_id": client_id, "text": text},
            )
            seq = body.get("seq") if isinstance(body, dict) else None
            send_ok = 200 <= status < 300 and isinstance(seq, int) and seq > 0
            read_status = 0
            readback = False
            if send_ok:
                read_status, read_body = self._service_json(
                    "GET",
                    f"/channels/{urllib.parse.quote(channel, safe='')}/messages?after_seq={seq - 1}&limit=1",
                )
                rows = read_body.get("messages") if isinstance(read_body, dict) else None
                readback = (
                    200 <= read_status < 300
                    and isinstance(rows, list)
                    and len(rows) == 1
                    and rows[0].get("client_msg_id") == client_id
                    and rows[0].get("body") == text
                    and rows[0].get("seq") == seq
                )
            records.append(
                {
                    "channel_id": channel,
                    "client_msg_id": client_id,
                    "send_status": status,
                    "seq": seq,
                    "read_status": read_status,
                    "correct": send_ok and readback,
                }
            )
        completed = sum(1 for row in records if 200 <= row["send_status"] < 300)
        correct = sum(1 for row in records if row["correct"])
        return {
            "scheduled": len(records),
            "attempted": len(records),
            "completed": completed,
            "correct": correct,
            "records": records,
        }

    def challenge(self, challenge_id: str) -> dict[str, Any]:
        with self.lock:
            state: dict[str, Any] | None = None
            if RECEIPT.is_file():
                try:
                    state = json.loads(RECEIPT.read_text())
                except (OSError, json.JSONDecodeError) as exc:
                    raise RuntimeError(
                        f"verifier broker: durable restart journal is unreadable: {exc}"
                    ) from exc
                if not isinstance(state, dict) or state.get("challenge_id") != challenge_id:
                    raise RuntimeError("verifier broker: a different challenge already started")
                if state.get("phase") == "completed":
                    receipt = state.get("receipt")
                    if not isinstance(receipt, dict):
                        raise RuntimeError("verifier broker: completed journal lacks receipt")
                    return {**receipt, "replayed": True}
                deadline = time.monotonic() + self.timeout_s
                if state.get("phase") not in {"prepared", "deleted"}:
                    raise RuntimeError("verifier broker: durable restart journal has invalid phase")
                old = state.get("old_pod")
                if not isinstance(old, dict) or not isinstance(old.get("uid"), str):
                    raise RuntimeError("verifier broker: durable restart journal lacks old pod identity")
            else:
                deadline = time.monotonic() + self.timeout_s
                old = self._wait_current_identity(
                    deadline=deadline,
                    context="initial",
                    allow_not_found=False,
                )
                if old is None:  # pragma: no cover - forbidden by allow_not_found=False
                    raise RuntimeError("verifier broker: fixed target initial pod is absent")
                state = {
                    "schema_version": 1,
                    "challenge_id": challenge_id,
                    "phase": "prepared",
                    "old_pod": old,
                }
                _persist_state(state)

            current = self._wait_current_identity(
                deadline=deadline,
                context="resume",
                allow_not_found=True,
            )
            current_uid = current["uid"] if current is not None else None
            delete_status = state.get("delete_status")
            if current_uid == old["uid"]:
                delete_status, _ = self._kube("DELETE")
                if delete_status not in {200, 202}:
                    raise RuntimeError(
                        f"verifier broker: fixed target DELETE failed: HTTP {delete_status}"
                    )
                state = {**state, "phase": "deleted", "delete_status": delete_status}
                _persist_state(state)
            elif delete_status not in {200, 202}:
                # The target already changed after a crash between DELETE and the
                # journal update.  Record the observed mutation without issuing a
                # second delete.
                delete_status = 202
                state = {**state, "phase": "deleted", "delete_status": delete_status}
                _persist_state(state)

            new = self._wait_new_ready(old["uid"], deadline=deadline)
            receipt = {
                "schema_version": 1,
                "challenge_id": challenge_id,
                "actor": "verifier",
                "target_pod": self.target_pod,
                "target_service": self.target_service,
                "action": "delete_fixed_pod",
                "delete_status": delete_status,
                "old_pod": old,
                "new_pod": new,
                "ready": True,
                "replayed": False,
            }
            _persist_state(
                {
                    "schema_version": 1,
                    "challenge_id": challenge_id,
                    "phase": "completed",
                    "old_pod": old,
                    "delete_status": delete_status,
                    "receipt": receipt,
                }
            )
            return receipt


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "verifier-broker/1"

    @property
    def broker(self) -> Broker:
        return self.server.broker  # type: ignore[attr-defined]

    def _reply(self, status: int, payload: Any) -> None:
        body = _json_bytes(payload)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/healthz":
            self._reply(200, {"ok": True})
        else:
            self._reply(404, {"error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/challenge":
            self._reply(404, {"error": "not_found"})
            return
        supplied = self.headers.get(HEADER, "")
        import hmac

        if not hmac.compare_digest(supplied, self.broker.grader_token):
            self._reply(403, {"error": "grader_access_forbidden"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 1 <= length <= 4096:
                raise ValueError("invalid request size")
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict) or set(payload) != {"schema_version", "challenge_id"}:
                raise ValueError("invalid request shape")
            if payload["schema_version"] != 1 or not isinstance(payload["challenge_id"], str):
                raise ValueError("invalid challenge identity")
            receipt = self.broker.challenge(payload["challenge_id"])
        except ValueError as exc:
            self._reply(400, {"error": str(exc)})
            return
        except Exception as exc:
            self._reply(500, {"error": type(exc).__name__, "detail": str(exc)[:500]})
            return
        self._reply(200, receipt)

    def log_message(self, format: str, *args: Any) -> None:
        print(f"verifier broker: {format % args}", flush=True)


def main() -> None:
    broker = Broker()
    server = http.server.ThreadingHTTPServer(("0.0.0.0", 9190), Handler)
    server.broker = broker  # type: ignore[attr-defined]
    server.serve_forever()


if __name__ == "__main__":
    main()
