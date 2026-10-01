"""Root-only Unix-socket transport for finalized grader evidence.

The Harbor agent executes only in the ``main`` container as uid 10001.  The
verifier later executes there as root.  This broker keeps the grader bearer
capability in a separate sidecar and exposes a 0600 Unix socket on a shared
emptyDir, so the verifier can finalize and fetch evidence without placing the
capability in the agent's exec container.
"""

from __future__ import annotations

import http.client
import json
import os
import socketserver
from http.server import BaseHTTPRequestHandler
from pathlib import Path


TOKEN_FILE = Path(
    os.environ.get("GRADER_ACCESS_TOKEN_FILE", "/run/grader-access/token")
)
SOCKET_PATH = Path(
    os.environ.get("GRADER_BROKER_SOCKET", "/run/verifier-broker/grader.sock")
)
UPSTREAM_HOST = os.environ.get("GRADER_UPSTREAM_HOST", "loadgen")
UPSTREAM_PORT = int(os.environ.get("GRADER_UPSTREAM_PORT", "9100"))
HEADER = "X-SRE-World-Grader-Access"
ALLOWED_REQUESTS = {
    # Harbor's root environment healthcheck pins the episode clock through this
    # broker only after evaluating readiness. Brokering keeps the bearer
    # capability out of the agent's exec container and stops the agent from
    # choosing t0.
    ("POST", "/grader/episode-start"),
    ("GET", "/grader/episode_done"),
    ("GET", "/grader/bundle"),
}


def _token() -> str:
    try:
        value = TOKEN_FILE.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RuntimeError(
            f"grader-broker: capability unavailable at {TOKEN_FILE}: {exc}"
        ) from exc
    if len(value) < 32:
        raise RuntimeError("grader-broker: capability is missing or too short")
    return value


TOKEN = _token()


class Handler(BaseHTTPRequestHandler):
    def _json(self, status: int, payload: dict[str, object]) -> None:
        body = json.dumps(payload, sort_keys=True).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _forward(self) -> None:
        if (self.command, self.path) not in ALLOWED_REQUESTS:
            self._json(404, {"error": "not_found"})
            return
        connection = http.client.HTTPConnection(
            UPSTREAM_HOST, UPSTREAM_PORT, timeout=30
        )
        try:
            connection.request(
                self.command,
                self.path,
                headers={HEADER: TOKEN},
            )
            response = connection.getresponse()
            body = response.read()
            self.send_response(response.status)
            content_type = response.getheader("Content-Type")
            if content_type:
                self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (OSError, http.client.HTTPException) as exc:
            self._json(
                502,
                {"error": f"grader_upstream_failure: {type(exc).__name__}: {exc}"},
            )
        finally:
            connection.close()

    def do_GET(self) -> None:
        self._forward()

    def do_POST(self) -> None:
        self._forward()

    def log_message(self, fmt: str, *args: object) -> None:
        # Never log request headers or response bodies. Promtail may collect
        # this container's stdout, and the agent can query the redacted bridge.
        print(f"grader-broker: {fmt % args}", flush=True)


class ThreadingUnixHTTPServer(
    socketserver.ThreadingMixIn, socketserver.UnixStreamServer
):
    daemon_threads = True


def main() -> None:
    SOCKET_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if SOCKET_PATH.exists():
        if not SOCKET_PATH.is_socket():
            raise RuntimeError(
                f"grader-broker: refusing to replace non-socket path {SOCKET_PATH}"
            )
        SOCKET_PATH.unlink()
    server = ThreadingUnixHTTPServer(str(SOCKET_PATH), Handler)
    os.chmod(SOCKET_PATH, 0o600)
    actual_mode = SOCKET_PATH.stat().st_mode & 0o777
    if actual_mode != 0o600:
        raise RuntimeError(
            f"grader-broker: socket mode is {actual_mode:o}, expected 600"
        )
    print(f"grader-broker: listening on root-only Unix socket {SOCKET_PATH}", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        try:
            SOCKET_PATH.unlink()
        except FileNotFoundError:
            pass


if __name__ == "__main__":
    main()
