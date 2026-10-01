"""Fixed-target trusted rollout broker for Saleor build-capable tasks.

The agent can request a rollout but cannot choose its target or patch a
Deployment.  Source syntax is checked from the task-owned PVC before any
workload mutation.  A failed check leaves the running revision untouched so
the agent can repair the same files and retry.
"""

from __future__ import annotations

import hashlib
import json
import os
import py_compile
import stat
import ssl
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from typing import Any


class BrokerError(RuntimeError):
    pass


def _source_paths() -> list[str]:
    try:
        raw = json.loads(os.environ["SOURCE_PATHS_JSON"])
    except (KeyError, json.JSONDecodeError) as exc:
        raise BrokerError(f"SOURCE_PATHS_JSON is missing or malformed: {exc}") from exc
    if not isinstance(raw, list) or not raw:
        raise BrokerError("SOURCE_PATHS_JSON must be a non-empty list")
    prefix = ("services", "app", "src", "saleor")
    paths: list[str] = []
    for value in raw:
        path = PurePosixPath(value) if isinstance(value, str) else None
        if (
            path is None
            or path.is_absolute()
            or value != path.as_posix()
            or path.parts[:4] != prefix
            or len(path.parts) <= 4
            or path.suffix != ".py"
        ):
            raise BrokerError(f"invalid Saleor source path: {value!r}")
        paths.append(value)
    if len(paths) != len(set(paths)) or len(paths) > 8:
        raise BrokerError("source paths must be unique and contain at most eight files")
    return paths


class KubernetesClient:
    def __init__(self, target: str) -> None:
        service_account = Path("/var/run/secrets/kubernetes.io/serviceaccount")
        try:
            self.token = (service_account / "token").read_text().strip()
            self.namespace = (service_account / "namespace").read_text().strip()
        except OSError as exc:
            raise BrokerError(f"cannot read broker service-account projection: {exc}") from exc
        self.target = target
        self.context = ssl.create_default_context(cafile=str(service_account / "ca.crt"))
        self.base = "https://kubernetes.default.svc"

    @property
    def path(self) -> str:
        return (
            f"/apis/apps/v1/namespaces/{self.namespace}/deployments/{self.target}"
        )

    @property
    def snapshot_path(self) -> str:
        return f"/api/v1/namespaces/{self.namespace}/configmaps/saleor-agent-snapshot"

    def request(
        self,
        method: str,
        payload: Any | None = None,
        *,
        content_type: str = "application/merge-patch+json",
        path: str | None = None,
    ) -> dict[str, Any]:
        data = None if payload is None else json.dumps(payload).encode()
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
        }
        if data is not None:
            headers["Content-Type"] = content_type
        request = urllib.request.Request(
            self.base + (path or self.path), data=data, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(
                request, context=self.context, timeout=10
            ) as response:
                document = json.loads(response.read())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")
            raise BrokerError(
                f"Kubernetes {method} {self.target} failed: HTTP {exc.code}: {detail[:1000]}"
            ) from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise BrokerError(f"Kubernetes {method} {self.target} failed: {exc}") from exc
        if not isinstance(document, dict):
            raise BrokerError(f"Kubernetes {method} returned a non-object response")
        return document


class Broker:
    def __init__(self) -> None:
        role = os.environ.get("BUILD_TARGET_ROLE", "")
        if role not in {"api", "worker"}:
            raise BrokerError(f"BUILD_TARGET_ROLE must be api or worker, got {role!r}")
        self.target = f"saleor-{role}"
        self.paths = _source_paths()
        self.client = KubernetesClient(self.target)
        self.timeout = float(os.environ.get("REBUILD_TIMEOUT_S", "300"))
        if self.timeout <= 0:
            raise BrokerError("REBUILD_TIMEOUT_S must be positive")
        self.lock = threading.Lock()

    @staticmethod
    def _read_source(logical: str) -> bytes:
        path = Path("/source") / logical
        try:
            info = path.lstat()
        except OSError as exc:
            raise BrokerError(f"cannot stat declared source file {logical}: {exc}") from exc
        if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise BrokerError(f"declared source file is missing or unsafe: {logical}")
        if info.st_size > 1_048_576:
            raise BrokerError(f"declared source file exceeds 1 MiB: {logical}")
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise BrokerError(f"cannot read declared source file {logical}: {exc}") from exc
        if len(data) != info.st_size:
            raise BrokerError(f"declared source file changed while read: {logical}")
        return data

    def _snapshot(self) -> tuple[str, dict[str, str]]:
        payloads = [(logical, self._read_source(logical)) for logical in sorted(self.paths)]
        if sum(len(data) for _logical, data in payloads) > 786_432:
            raise BrokerError("declared source snapshot exceeds the bounded ConfigMap budget")
        digest = hashlib.sha256()
        digest.update(b"sre-world-saleor-source-snapshot-v1\0")
        for logical, data in payloads:
            encoded = logical.encode()
            digest.update(len(encoded).to_bytes(4, "big"))
            digest.update(encoded)
            digest.update(len(data).to_bytes(8, "big"))
            digest.update(data)
        source_sha256 = digest.hexdigest()
        snapshot_paths: dict[str, str] = {}
        config_data: dict[str, str] = {}
        with tempfile.TemporaryDirectory(prefix="saleor-source-", dir="/tmp") as temporary:
            root = Path(temporary)
            for logical, expected in payloads:
                key = f"source-{hashlib.sha256(logical.encode()).hexdigest()}.py"
                if key in config_data:
                    raise BrokerError(f"trusted source snapshot key collision: {logical}")
                try:
                    text = expected.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise BrokerError(f"declared source file is not UTF-8: {logical}") from exc
                path = root / key
                path.write_bytes(expected)
                path.chmod(0o444)
                try:
                    cache = root / f"{key}.pyc"
                    py_compile.compile(str(path), cfile=str(cache), doraise=True)
                except py_compile.PyCompileError as exc:
                    raise BrokerError(
                        f"source validation failed for {logical}: {exc.msg}"
                    ) from exc
                snapshot_paths[logical] = key
                config_data[key] = text

        before = self.client.request("GET", path=self.client.snapshot_path)
        metadata = before.get("metadata") or {}
        resource_version = metadata.get("resourceVersion")
        if not isinstance(resource_version, str) or not resource_version:
            raise BrokerError("trusted source snapshot ConfigMap has no resourceVersion")
        annotations = metadata.get("annotations")
        operations: list[dict[str, Any]] = [
            {"op": "test", "path": "/metadata/resourceVersion", "value": resource_version}
        ]
        if annotations is None:
            operations.append({"op": "add", "path": "/metadata/annotations", "value": {}})
        elif not isinstance(annotations, dict):
            raise BrokerError("trusted source snapshot ConfigMap annotations are malformed")
        operations.extend(
            [
                {
                    "op": "add",
                    "path": "/metadata/annotations/sre-world.abundant.ai~1source-sha256",
                    "value": source_sha256,
                },
                {"op": "replace", "path": "/data", "value": config_data},
            ]
        )
        snapshot = self.client.request(
            "PATCH",
            operations,
            content_type="application/json-patch+json",
            path=self.client.snapshot_path,
        )
        snapshot_metadata = snapshot.get("metadata") or {}
        if (
            (snapshot_metadata.get("annotations") or {}).get(
                "sre-world.abundant.ai/source-sha256"
            )
            != source_sha256
            or snapshot.get("data") != config_data
        ):
            raise BrokerError("trusted source snapshot ConfigMap identity mismatch")
        return source_sha256, snapshot_paths

    def _rollout_patch(
        self,
        deployment: dict[str, Any],
        *,
        revision: str,
        source_sha256: str,
        snapshot_paths: dict[str, str],
    ) -> list[dict[str, Any]]:
        metadata = deployment.get("metadata") or {}
        resource_version = metadata.get("resourceVersion")
        if not isinstance(resource_version, str) or not resource_version:
            raise BrokerError("target Deployment has no resourceVersion")
        template = ((deployment.get("spec") or {}).get("template") or {})
        template_metadata = template.get("metadata") or {}
        annotations = template_metadata.get("annotations")
        operations: list[dict[str, Any]] = [
            {"op": "test", "path": "/metadata/resourceVersion", "value": resource_version}
        ]
        if annotations is None:
            operations.append(
                {"op": "add", "path": "/spec/template/metadata/annotations", "value": {}}
            )
        elif not isinstance(annotations, dict):
            raise BrokerError("target Deployment pod-template annotations are malformed")
        operations.extend(
            [
                {
                    "op": "add",
                    "path": "/spec/template/metadata/annotations/sre-world.abundant.ai~1source-revision",
                    "value": revision,
                },
                {
                    "op": "add",
                    "path": "/spec/template/metadata/annotations/sre-world.abundant.ai~1source-sha256",
                    "value": source_sha256,
                },
            ]
        )
        containers = ((template.get("spec") or {}).get("containers") or [])
        container_name = "api" if self.target == "saleor-api" else "worker"
        matches = [
            (index, value)
            for index, value in enumerate(containers)
            if isinstance(value, dict) and value.get("name") == container_name
        ]
        if len(matches) != 1:
            raise BrokerError(
                f"target Deployment must contain exactly one {container_name!r} container"
            )
        container_index, container = matches[0]
        mounts = container.get("volumeMounts") or []
        for logical, snapshot in snapshot_paths.items():
            mount_path = f"/app/{logical.removeprefix('services/app/src/')}"
            mount_matches = [
                index
                for index, value in enumerate(mounts)
                if isinstance(value, dict)
                and value.get("name") in {"agent-source", "agent-snapshot"}
                and value.get("mountPath") == mount_path
            ]
            if len(mount_matches) != 1:
                raise BrokerError(
                    f"target Deployment has no unique source mount for {logical}"
                )
            operations.append(
                {
                    "op": "replace",
                    "path": (
                        f"/spec/template/spec/containers/{container_index}/volumeMounts/"
                        f"{mount_matches[0]}"
                    ),
                    "value": {
                        "name": "agent-snapshot",
                        "mountPath": mount_path,
                        "subPath": snapshot,
                        "readOnly": True,
                    },
                }
            )
        return operations

    def rebuild(self) -> dict[str, Any]:
        if not self.lock.acquire(blocking=False):
            raise BrokerError("trusted rollout already in progress")
        try:
            source_sha256, snapshot_paths = self._snapshot()
            before = self.client.request("GET")
            old_generation = int((before.get("metadata") or {}).get("generation") or 0)
            revision = str(time.time_ns())
            patched = self.client.request(
                "PATCH",
                self._rollout_patch(
                    before,
                    revision=revision,
                    source_sha256=source_sha256,
                    snapshot_paths=snapshot_paths,
                ),
                content_type="application/json-patch+json",
            )
            generation = int((patched.get("metadata") or {}).get("generation") or 0)
            if generation <= old_generation:
                raise BrokerError(
                    f"rollout patch did not advance generation: {old_generation} -> {generation}"
                )
            deadline = time.monotonic() + self.timeout
            last: dict[str, Any] = {}
            while time.monotonic() < deadline:
                current = self.client.request("GET")
                status = current.get("status") or {}
                spec = current.get("spec") or {}
                desired = int(spec.get("replicas") or 1)
                last = {
                    "observed_generation": int(status.get("observedGeneration") or 0),
                    "updated_replicas": int(status.get("updatedReplicas") or 0),
                    "ready_replicas": int(status.get("readyReplicas") or 0),
                    "available_replicas": int(status.get("availableReplicas") or 0),
                    "desired_replicas": desired,
                }
                if (
                    last["observed_generation"] >= generation
                    and last["updated_replicas"] == desired
                    and last["ready_replicas"] == desired
                    and last["available_replicas"] == desired
                ):
                    return {
                        "ok": True,
                        "target": self.target,
                        "generation": generation,
                        "source_revision": revision,
                        "source_sha256": source_sha256,
                        "source_snapshot_paths": snapshot_paths,
                    }
                time.sleep(1)
            raise BrokerError(f"timed out waiting for trusted rollout; last={last}")
        finally:
            self.lock.release()


def handler(broker: Broker) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "sre-world-saleor-rebuild-broker/1"

        def send_json(self, status: int, payload: dict[str, Any]) -> None:
            encoded = json.dumps(payload, sort_keys=True).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def do_GET(self) -> None:  # noqa: N802
            self.send_json(200, {"ok": True}) if self.path == "/healthz" else self.send_json(
                404, {"ok": False, "error": "not_found"}
            )

        def do_POST(self) -> None:  # noqa: N802
            if self.path != "/rebuild":
                self.send_json(404, {"ok": False, "error": "not_found"})
                return
            if (
                self.headers.get("Transfer-Encoding")
                or self.headers.get("Content-Length", "0") != "0"
            ):
                self.send_json(400, {"ok": False, "error": "body_forbidden"})
                return
            try:
                self.send_json(200, broker.rebuild())
            except BrokerError as exc:
                status = 422 if "source validation failed" in str(exc) else 502
                self.send_json(status, {"ok": False, "error": str(exc)})

        def log_message(self, fmt: str, *args: Any) -> None:
            print(f"[saleor-rebuild-broker] {fmt % args}", flush=True)

    return Handler


def main() -> None:
    try:
        broker = Broker()
    except (BrokerError, ValueError) as exc:
        raise SystemExit(f"saleor-rebuild-broker: FATAL: {exc}") from exc
    print(f"saleor-rebuild-broker: target={broker.target} paths={broker.paths}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", 9180), handler(broker)).serve_forever()


if __name__ == "__main__":
    main()
