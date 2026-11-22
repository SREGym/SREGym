"""Behavior and durable-data checks that complement traffic measurements."""

from __future__ import annotations

import contextlib
import hashlib
import http.client
import io
import json
import subprocess
import time
import urllib.parse
from abc import ABC, abstractmethod
from collections import Counter
from dataclasses import dataclass, field


@dataclass(frozen=True)
class RecoveryCheckResult:
    reason: str | None = None
    detail: dict = field(default_factory=dict)

    @property
    def success(self) -> bool:
        return self.reason is None


class _HttpResponseBuffer:
    def __init__(self, response: str):
        self.response = response.encode()

    def makefile(self, mode: str):
        return io.BytesIO(self.response)


class ApplicationRecoveryCheck(ABC):
    """Preserve baseline records without fixing pod identities or database IDs."""

    collections: dict[str, tuple[str, str, str]]

    def __init__(self, app):
        self.namespace = app.namespace
        self.frontend_service = app.frontend_service
        self.frontend_port = app.frontend_port
        self.workload_job = getattr(getattr(app, "wrk", None), "job_name", "wrk2-job")
        self._probe_pod: str | None = None
        self.baseline: dict[str, Counter] | None = None

    def _kubectl(self, *args: str) -> str:
        result = subprocess.run(["kubectl", "-n", self.namespace, *args], capture_output=True, check=True, timeout=60)
        return result.stdout.decode()

    def read_collection(self, name: str, query: dict | None = None) -> list[dict]:
        service, database, collection = self.collections[name]
        # Internal MongoDB IDs and login secrets are not public record identity.
        script = (
            f"print(JSON.stringify(db.getCollection({json.dumps(collection)}).find("
            f"{json.dumps(query or {})},{{_id:0,password:0,salt:0}}).toArray()));"
        )
        rows = json.loads(
            self._kubectl("exec", f"service/{service}", "--", "mongo", "--quiet", database, "--eval", script)
        )
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            raise ValueError(f"Invalid application records in {name}")
        return rows

    @staticmethod
    def fingerprints(rows: list[dict]) -> Counter:
        return Counter(
            hashlib.sha256(json.dumps(row, sort_keys=True, separators=(",", ":")).encode()).hexdigest() for row in rows
        )

    @contextlib.contextmanager
    def frontend(self):
        """Use the protected workload's network path through the actual Service."""
        job = json.loads(self._kubectl("get", "job", self.workload_job, "-o", "json"))
        pods = json.loads(self._kubectl("get", "pods", "-l", f"job-name={self.workload_job}", "-o", "json"))
        for pod in pods["items"]:
            owners = pod["metadata"].get("ownerReferences", [])
            if (
                not pod["metadata"].get("deletionTimestamp")
                and pod.get("status", {}).get("phase") == "Running"
                and any(owner.get("kind") == "Job" and owner.get("uid") == job["metadata"]["uid"] for owner in owners)
            ):
                self._probe_pod = pod["metadata"]["name"]
                break
        if self._probe_pod is None:
            raise RuntimeError("No running pod belongs to the protected workload Job")
        try:
            yield f"http://{self.frontend_service}:{self.frontend_port}"
        finally:
            self._probe_pod = None

    def request(self, base: str, path: str, params: dict, *, post: bool = False) -> str:
        if self._probe_pod is None:
            raise RuntimeError("Application request requires a workload probe context")
        encoded = urllib.parse.urlencode(params)
        endpoint = urllib.parse.urlsplit(base)
        body = encoded if post else ""
        target = path + ("" if post else "?" + encoded)
        payload = (
            f"{'POST' if post else 'GET'} {target} HTTP/1.0\r\nHost: {endpoint.netloc}\r\n"
            "Connection: close\r\nAccept-Encoding: identity\r\nContent-Type: application/x-www-form-urlencoded\r\n"
            f"Content-Length: {len(body.encode())}\r\n\r\n{body}"
        )
        # The published wrk2 image already contains Bash and coreutils, but no
        # HTTP client. Tunnel bytes through its socket and use Python's parser.
        raw = self._kubectl(
            "exec",
            self._probe_pod,
            "--",
            "timeout",
            "15",
            "bash",
            "-c",
            'exec 3<>"/dev/tcp/$1/$2"; printf "%s" "$3" >&3; cat <&3',
            "application-probe",
            endpoint.hostname,
            str(endpoint.port),
            payload,
        )
        response = http.client.HTTPResponse(_HttpResponseBuffer(raw))
        response.begin()
        if response.status != 200:
            raise ValueError(f"Application request returned HTTP {response.status}")
        return response.read().decode()

    def capture_baseline(self) -> None:
        records = {name: self.read_collection(name) for name in self.collections}
        with self.frontend() as url:
            self._verify_with_retry(url, records)
        # A healthy fresh app can have no posts yet. Include the probe's real
        # write in the baseline rather than rejecting an empty initial store.
        records = {name: self.read_collection(name) for name in self.collections}
        self.baseline = {name: self.fingerprints(rows) for name, rows in records.items()}

    def evaluate(self) -> RecoveryCheckResult:
        if self.baseline is None:
            raise RuntimeError("Application recovery check has no pre-fault baseline")
        records = {name: self.read_collection(name) for name in self.collections}
        missing = {
            name: sum((before - self.fingerprints(records[name])).values()) for name, before in self.baseline.items()
        }
        if any(missing.values()):
            return RecoveryCheckResult("application_data_changed", {"missing_or_changed": missing})
        try:
            with self.frontend() as url:
                self._verify_with_retry(url, records)
        except (ValueError, TypeError, KeyError, AttributeError) as error:
            return RecoveryCheckResult("application_response_invalid", {"error": str(error)})
        return RecoveryCheckResult(
            detail={"preserved_records": {name: sum(rows.values()) for name, rows in self.baseline.items()}}
        )

    def _verify_with_retry(self, url: str, records: dict[str, list[dict]]) -> None:
        # Retry the whole transaction with fresh identities. Retrying only its
        # write can duplicate an operation whose response was lost.
        for attempt in range(3):
            try:
                self.verify_requests(url, records)
                return
            except (ValueError, TypeError, KeyError, AttributeError):
                if attempt == 2:
                    raise
                time.sleep(1)

    @abstractmethod
    def verify_requests(self, url: str, records: dict[str, list[dict]]) -> None:
        """Require real application reads and a fresh persistent write."""
