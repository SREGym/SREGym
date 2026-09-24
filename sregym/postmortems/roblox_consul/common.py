import contextlib
import json
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer


def request(url, method="GET", data=None, timeout=5, headers=None, raw=False):
    headers = dict(headers or {})
    if data is not None and not isinstance(data, bytes):
        data = json.dumps(data).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as response:
        payload = response.read()
    return payload if raw else json.loads(payload)


def respond(handler, payload, status=200):
    data = json.dumps(payload).encode()
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    with contextlib.suppress(BrokenPipeError, ConnectionResetError):
        handler.wfile.write(data)


def body(handler):
    length = int(handler.headers.get("Content-Length", 0))
    if length > 65536:
        raise ValueError("request too large")
    return json.loads(handler.rfile.read(length) or b"{}")


class Server(ThreadingHTTPServer):
    request_queue_size = 256
    daemon_threads = True
