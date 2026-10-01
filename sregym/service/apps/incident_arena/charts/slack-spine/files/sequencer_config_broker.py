"""Closed fixed-target broker for durable svc-message sequencer mode changes."""

from __future__ import annotations

import json
import os
import ssl
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


class BrokerError(RuntimeError):
    pass


class KubernetesClient:
    def __init__(self, *, api_base: str, token: str, ca_path: str, namespace: str) -> None:
        if not all((api_base, token, ca_path, namespace)):
            raise BrokerError("Kubernetes client configuration is incomplete")
        self.api_base = api_base.rstrip("/")
        self.token = token
        self.namespace = namespace
        self.context = ssl.create_default_context(cafile=ca_path)

    def _request(self, method: str, path: str, payload: Any | None = None) -> tuple[int, Any]:
        data = None if payload is None else json.dumps(payload).encode()
        headers = {"Authorization": f"Bearer {self.token}", "Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/strategic-merge-patch+json"
        request = urllib.request.Request(self.api_base + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, context=self.context, timeout=10) as response:
                raw = response.read()
                return response.status, json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:1000]
            if exc.code == 404:
                return 404, None
            raise BrokerError(
                f"Kubernetes {method} {path} failed: HTTP {exc.code}: {detail}"
            ) from exc
        except OSError as exc:
            raise BrokerError(f"Kubernetes {method} {path} failed: {exc}") from exc

    def get_statefulset(self) -> dict[str, Any]:
        status, payload = self._request(
            "GET", f"/apis/apps/v1/namespaces/{self.namespace}/statefulsets/svc-message"
        )
        if status != 200 or not isinstance(payload, dict):
            raise BrokerError(f"StatefulSet read returned HTTP {status}")
        return payload

    def patch_mode(self, mode: str) -> dict[str, Any]:
        status, payload = self._request(
            "PATCH",
            f"/apis/apps/v1/namespaces/{self.namespace}/statefulsets/svc-message",
            {"spec": {"template": {"spec": {"containers": [{
                "name": "app", "env": [{"name": "SEQUENCER_MODE", "value": mode}]
            }]}}}},
        )
        if status != 200 or not isinstance(payload, dict):
            raise BrokerError(f"StatefulSet patch returned HTTP {status}")
        return payload

    def get_pod(self) -> dict[str, Any] | None:
        status, payload = self._request(
            "GET", f"/api/v1/namespaces/{self.namespace}/pods/svc-message-0"
        )
        if status == 404:
            return None
        if status != 200 or not isinstance(payload, dict):
            raise BrokerError(f"target pod read returned HTTP {status}")
        return payload


def _app_env(workload: dict[str, Any]) -> dict[str, str]:
    containers = (((workload.get("spec") or {}).get("template") or {}).get("spec") or {}).get("containers") or []
    app = next((item for item in containers if item.get("name") == "app"), None)
    if not isinstance(app, dict):
        raise BrokerError("svc-message lacks the app container")
    env = app.get("env") or []
    if not isinstance(env, list):
        raise BrokerError("svc-message app env is malformed")
    return {
        item["name"]: item["value"]
        for item in env
        if isinstance(item, dict)
        and isinstance(item.get("name"), str)
        and isinstance(item.get("value"), str)
    }


def _pod_state(pod: dict[str, Any]) -> tuple[str, bool]:
    uid = ((pod.get("metadata") or {}).get("uid"))
    statuses = ((pod.get("status") or {}).get("containerStatuses") or [])
    app = next((item for item in statuses if item.get("name") == "app"), None)
    if not isinstance(uid, str) or not uid:
        raise BrokerError("svc-message pod lacks a UID")
    return uid, isinstance(app, dict) and app.get("ready") is True


class SequencerConfigBroker:
    def __init__(self, client: Any, timeout_seconds: float = 180, poll_seconds: float = 1) -> None:
        if timeout_seconds <= 0 or poll_seconds <= 0:
            raise BrokerError("positive broker timeouts are required")
        self.client = client
        self.timeout_seconds = timeout_seconds
        self.poll_seconds = poll_seconds
        self._lock = threading.Lock()

    def set_mode(self, mode: str) -> dict[str, Any]:
        if mode != "atomic":
            raise BrokerError("mode must be exactly atomic")
        if not self._lock.acquire(blocking=False):
            raise BrokerError("sequencer configuration change already in progress")
        try:
            before_workload = self.client.get_statefulset()
            old_pod = self.client.get_pod()
            if old_pod is None:
                raise BrokerError("target pod svc-message-0 does not exist before change")
            old_uid, _ = _pod_state(old_pod)
            before_mode = _app_env(before_workload).get("SEQUENCER_MODE", "atomic")
            patched = self.client.patch_mode(mode)
            if _app_env(patched).get("SEQUENCER_MODE") != mode:
                raise BrokerError("Kubernetes patch did not persist the requested mode")
            deadline = time.monotonic() + self.timeout_seconds
            last_uid: str | None = old_uid
            ready = False
            while time.monotonic() < deadline:
                pod = self.client.get_pod()
                if pod is None:
                    # A StatefulSet rollout normally has a short deletion window.
                    # Absence is valid only after the persisted patch; keep waiting
                    # for the fixed target to be recreated and become ready.
                    last_uid, ready = None, False
                    time.sleep(self.poll_seconds)
                    continue
                last_uid, ready = _pod_state(pod)
                if ready and (last_uid != old_uid or before_mode == mode):
                    return {
                        "ok": True,
                        "target": "svc-message",
                        "mode": mode,
                        "previous_mode": before_mode,
                        "old_uid": old_uid,
                        "new_uid": last_uid,
                        "restarted": last_uid != old_uid,
                    }
                time.sleep(self.poll_seconds)
            raise BrokerError(
                f"timed out waiting for ready svc-message-0; old_uid={old_uid} "
                f"last_uid={last_uid} ready={ready}"
            )
        finally:
            self._lock.release()


def _handler(broker: SequencerConfigBroker) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "sre-world-sequencer-config-broker/1"

        def _json(self, status: int, payload: dict[str, Any]) -> None:
            data = json.dumps(payload, sort_keys=True).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:  # noqa: N802
            if self.path != "/healthz":
                self._json(404, {"ok": False, "error": "not_found"})
                return
            self._json(200, {"ok": True})

        def do_POST(self) -> None:  # noqa: N802
            if self.path != "/v1/sequencer-mode":
                self._json(404, {"ok": False, "error": "not_found"})
                return
            try:
                length = int(self.headers.get("Content-Length", ""))
                if length < 1 or length > 64 or self.headers.get("Transfer-Encoding"):
                    raise ValueError("invalid request framing")
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict) or set(body) != {"mode"}:
                    raise ValueError("payload must contain exactly mode")
                mode = body["mode"]
                if not isinstance(mode, str) or mode != "atomic":
                    raise ValueError("mode must be exactly atomic")
            except (ValueError, json.JSONDecodeError) as exc:
                self._json(400, {"ok": False, "error": str(exc)})
                return
            try:
                self._json(200, broker.set_mode(mode))
            except BrokerError as exc:
                status = 409 if "already in progress" in str(exc) else 502
                self._json(status, {"ok": False, "error": str(exc)})

        def log_message(self, fmt: str, *args: Any) -> None:
            print(f"[sequencer-config-broker] {self.address_string()} {fmt % args}", flush=True)

    return Handler


def main() -> None:
    sa = Path(os.environ.get("K8S_SA_DIR", "/var/run/secrets/kubernetes.io/serviceaccount"))
    try:
        token = (sa / "token").read_text().strip()
        namespace = (sa / "namespace").read_text().strip()
    except OSError as exc:
        raise SystemExit(
            f"sequencer-config-broker: FATAL: cannot read service-account projection: {exc}"
        ) from exc
    timeout = float(os.environ.get("SEQUENCER_CONFIG_TIMEOUT_S", "180"))
    client = KubernetesClient(
        api_base=os.environ.get("K8S_API_BASE")
        or (
            f"https://{os.environ['KUBERNETES_SERVICE_HOST']}:"
            f"{os.environ.get('KUBERNETES_SERVICE_PORT_HTTPS', '443')}"
        ),
        token=token,
        ca_path=str(sa / "ca.crt"),
        namespace=namespace,
    )
    broker = SequencerConfigBroker(client, timeout_seconds=timeout)
    port = int(os.environ.get("SEQUENCER_CONFIG_PORT", "9181"))
    server = ThreadingHTTPServer(("0.0.0.0", port), _handler(broker))
    print(
        f"[sequencer-config-broker] fixed target=svc-message namespace={namespace} port={port}",
        flush=True,
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
