"""HTTP transport for probes executed in an application-client pod."""

import json
import urllib.error
import urllib.request
from pathlib import Path


class Client:
    def __init__(self, base):
        self.base = base
        self.headers = {}
        self.password = Path("/credentials/password").read_text()
        self.token = Path("/credentials/token").read_text()

    @staticmethod
    def require(condition, message):
        if not condition:
            raise RuntimeError(message)

    def call(self, method, path, data=None, headers=None, raw=False):
        hs = {**self.headers, **(headers or {})}
        if data is not None and not isinstance(data, bytes):
            data = json.dumps(data).encode()
            hs.setdefault("Content-Type", "application/json")
        req = urllib.request.Request(self.base + path, data=data, headers=hs, method=method)
        try:
            with urllib.request.urlopen(req, timeout=20) as response:
                body = response.read()
                self.response_headers = response.headers
                return body if raw else json.loads(body) if body else None
        except urllib.error.HTTPError as exc:
            # Retain API evidence, never headers containing access credentials.
            detail = exc.read().decode(errors="replace")[:1000]
            raise RuntimeError(f"{method} {path}: HTTP {exc.code}: {detail}") from None
