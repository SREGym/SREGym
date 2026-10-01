#!/usr/bin/env python3
"""Authenticated controller for the trusted setup/runtime proxy cutover."""

from __future__ import annotations

import json
import os
import ssl
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


AUTH_TOKEN_PATH = Path("/run/agent-egress-controller/auth/token")
SA_ROOT = Path("/var/run/secrets/kubernetes.io/serviceaccount")
DEPLOYMENT = "agent-egress-proxy"


def api_request(path: str, *, method: str = "GET", payload: dict | None = None):
    host = os.environ["KUBERNETES_SERVICE_HOST"]
    port = os.environ.get("KUBERNETES_SERVICE_PORT_HTTPS", "443")
    request = urllib.request.Request(
        f"https://{host}:{port}{path}",
        data=None if payload is None else json.dumps(payload).encode(),
        method=method,
        headers={
            "Authorization": f"Bearer {(SA_ROOT / 'token').read_text().strip()}",
            "Content-Type": "application/strategic-merge-patch+json",
        },
    )
    context = ssl.create_default_context(cafile=str(SA_ROOT / "ca.crt"))
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=context)
    )
    try:
        with opener.open(request, timeout=10) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def deployment_path(namespace: str, name: str) -> str:
    return f"/apis/apps/v1/namespaces/{namespace}/deployments/{name}"


def switch_phase(phase: str) -> dict:
    namespace = (SA_ROOT / "namespace").read_text().strip()
    proxy_path = deployment_path(namespace, DEPLOYMENT)
    status, _ = api_request(proxy_path)
    if status != 200:
        raise RuntimeError(f"cannot read proxy Deployment (HTTP {status})")
    status, _ = api_request(deployment_path(namespace, "main"))
    if status != 403:
        raise RuntimeError(f"RBAC is over-broad for main Deployment (HTTP {status})")

    proxy_config = f"/etc/agent-egress/{phase}-envoy.yaml"
    patch = {
        "spec": {
            "template": {
                "metadata": {"annotations": {"sre-world/egress-phase": phase}},
                "spec": {
                    "containers": [
                        {
                            "name": "proxy",
                            "args": [
                                "-c",
                                proxy_config,
                                "--service-cluster",
                                DEPLOYMENT,
                            ],
                        }
                    ]
                },
            }
        }
    }
    status, body = api_request(proxy_path, method="PATCH", payload=patch)
    if status != 200:
        raise RuntimeError(f"proxy patch failed (HTTP {status}): {body}")

    deadline = time.monotonic() + 120
    last = None
    while time.monotonic() < deadline:
        status, body = api_request(proxy_path)
        if status != 200:
            raise RuntimeError(f"proxy rollout read failed (HTTP {status}): {body}")
        spec = body.get("spec") or {}
        observed = body.get("status") or {}
        containers = ((spec.get("template") or {}).get("spec") or {}).get(
            "containers"
        ) or []
        proxy = next((item for item in containers if item.get("name") == "proxy"), {})
        desired = spec.get("replicas", 1)
        last = {
            "generation": (body.get("metadata") or {}).get("generation"),
            "observedGeneration": observed.get("observedGeneration"),
            "desired": desired,
            "updated": observed.get("updatedReplicas", 0),
            "ready": observed.get("readyReplicas", 0),
            "available": observed.get("availableReplicas", 0),
            "unavailable": observed.get("unavailableReplicas", 0),
            "args": proxy.get("args") or [],
        }
        if (
            last["generation"] == last["observedGeneration"]
            and desired == last["updated"] == last["ready"] == last["available"] == 1
            and last["unavailable"] == 0
            and proxy_config in last["args"]
        ):
            return last
        time.sleep(1)
    raise RuntimeError(f"proxy rollout timed out: {last}")


class Handler(BaseHTTPRequestHandler):
    server_version = "agent-egress-controller/1"

    def reply(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, sort_keys=True).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        self.reply(200, {"ok": True}) if self.path == "/healthz" else self.reply(
            404, {"error": "not found"}
        )

    def do_POST(self) -> None:  # noqa: N802
        expected = f"Bearer {AUTH_TOKEN_PATH.read_text().strip()}"
        if self.headers.get("Authorization") != expected:
            self.reply(403, {"error": "forbidden"})
            return
        phase = self.path.removeprefix("/phase/")
        if phase not in {"bootstrap", "runtime"} or self.path != f"/phase/{phase}":
            self.reply(404, {"error": "unknown phase"})
            return
        try:
            state = switch_phase(phase)
        except Exception as exc:
            self.reply(500, {"error": str(exc)})
            return
        self.reply(200, {"ok": True, "phase": phase, "rollout": state})

    def log_message(self, format: str, *args) -> None:
        print(f"agent-egress-controller: {format % args}", flush=True)


if __name__ == "__main__":
    if not AUTH_TOKEN_PATH.read_text().strip():
        raise SystemExit("agent-egress-controller: empty auth token")
    ThreadingHTTPServer(("0.0.0.0", 9191), Handler).serve_forever()
