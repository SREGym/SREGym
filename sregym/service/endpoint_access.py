"""Forward an application's selected loopback HTTP endpoints into an agent container.

This file is also copied into the public tools directory. It has no benchmark
answers or host configuration; the gateway chooses the destination ports.
"""

from __future__ import annotations

import http.client
import json
import os
import shlex
import sys
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

HOP_HEADERS = {"connection", "proxy-connection", "keep-alive", "transfer-encoding", "upgrade"}


class LengthReader:
    def __init__(self, stream, length):
        self.stream, self.remaining = stream, length

    def read(self, size=65536):
        data = self.stream.read(min(size, self.remaining)) if self.remaining else b""
        self.remaining -= len(data)
        return data


class ForwardHandler(BaseHTTPRequestHandler):
    """Stream uploads and responses, including companyctl's complete exports."""

    def log_message(self, *_args):
        pass

    def do_GET(self):
        destination = self.server.destination(self.path, self.command)
        if destination is None:
            self.send_error(403, "Endpoint unavailable")
            return
        host, port, path, headers = destination
        if self.headers.get("Transfer-Encoding"):
            self.send_error(411, "Content-Length required")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 0:
                raise ValueError("Negative length")
        except ValueError:
            self.send_error(400, "Invalid Content-Length")
            return
        connection = http.client.HTTPConnection(host, port, timeout=1900)
        try:
            forwarded = {k: v for k, v in self.headers.items() if k.lower() not in HOP_HEADERS | {"host"}}
            forwarded.update(headers)
            forwarded["Connection"] = "close"
            forwarded["Content-Length"] = str(length)
            connection.request(self.command, path, body=LengthReader(self.rfile, length), headers=forwarded)
            response = connection.getresponse()
            self.send_response(response.status)
            for key, value in response.getheaders():
                if key.lower() not in HOP_HEADERS:
                    self.send_header(key, value)
            self.send_header("Connection", "close")
            self.end_headers()
            while data := response.read1(65536):
                self.wfile.write(data)
                self.wfile.flush()
        except (OSError, http.client.HTTPException):
            # Once a response has begun, closing it preserves an interrupted
            # export rather than fabricating a successful complete stream.
            self.close_connection = True
        finally:
            connection.close()
        self.close_connection = True

    do_POST = do_GET
    do_PUT = do_GET
    do_DELETE = do_GET
    do_HEAD = do_GET
    do_PATCH = do_GET


class EndpointGateway(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, bind_host, ports, conductor_port, is_open=lambda: True):
        self.ports = frozenset(ports)
        self.conductor_port = conductor_port
        self.is_open = is_open
        super().__init__((bind_host, 0), ForwardHandler)
        self.thread = threading.Thread(target=self.serve_forever, daemon=True)

    def destination(self, path, method):
        prefix, separator, rest = path.lstrip("/").partition("/")
        if not separator or not prefix.isdecimal():
            return None
        port, target = int(prefix), "/" + rest
        if port == self.conductor_port:
            if method == "POST" and not self.is_open():
                return None
            if (method, urlsplit(target).path) not in {("GET", "/get_app"), ("GET", "/status"), ("POST", "/submit")}:
                return None
        elif port not in self.ports or not self.is_open():
            return None
        return "127.0.0.1", port, target, {}

    def start(self):
        self.thread.start()

    def close(self):
        if self.thread.is_alive():
            self.shutdown()
        self.server_close()
        if self.thread.ident is not None:
            self.thread.join(timeout=5)


@dataclass(frozen=True)
class EndpointAccess:
    public_dir: Path
    gateway_url: str
    container_record: Path
    deadline_monotonic: float

    def wrap_command(self, command):
        return "python3 /opt/company/endpoint_access.py -- " + shlex.quote(command)


def enter(command, config):
    """Keep the opening's localhost URLs and the normal conductor API intact."""
    if time.monotonic() >= config["deadline_monotonic"]:
        raise TimeoutError("The response deadline has passed")
    proxy = urlsplit(os.environ["HTTP_PROXY"])
    servers = []
    for port in config["ports"]:
        server = ThreadingHTTPServer(("127.0.0.1", port), ForwardHandler)
        server.daemon_threads = True
        # HTTP requests use the existing SREGym egress proxy. No direct host
        # connection or extra container route is required.
        server.destination = lambda path, method, port=port: (
            proxy.hostname,
            proxy.port,
            f"{config['gateway_url']}/{port}{path}",
            {},
        )
        servers.append(server)
        threading.Thread(target=server.serve_forever, daemon=True).start()
    os.environ["PATH"] = "/opt/company:" + os.environ["PATH"]
    os.environ["API_HOSTNAME"] = "127.0.0.1"
    os.environ["API_PORT"] = str(config["conductor_port"])
    os.environ.pop("MCP_SERVER_URL", None)
    for name in ("OPENING.md", "COMPANY-TOOLS.md"):
        Path(name).write_text((Path("/opt/company") / name).read_text())
    import subprocess

    try:
        if time.monotonic() >= config["deadline_monotonic"]:
            raise TimeoutError("The response deadline has passed")
        return subprocess.call(["bash", "-c", command])
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    config = json.loads(Path("/opt/company/access.json").read_text())
    raise SystemExit(enter(sys.argv[2], config))
