"""Exercise polling deadlines through the real Kubernetes HTTP transport."""

import contextlib
import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest
from kubernetes import client

from sregym.service.kubectl import KubeCtl


@pytest.fixture
def status_api(allow_kubernetes_test_server):
    state = SimpleNamespace(delay=0, errors=[], requests=[], read_body_delay=0)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            state.requests.append((self.path, dict(self.headers)))
            time.sleep(state.delay)
            code = state.errors.pop(0) if state.errors else (200 if self.path.endswith("/pods") else 404)
            if code == 200:
                body = {
                    "apiVersion": "v1",
                    "kind": "PodList",
                    "items": [
                        {
                            "metadata": {"name": "app"},
                            "status": {
                                "phase": "Running",
                                "containerStatuses": [
                                    {"name": "app", "image": "test", "imageID": "", "ready": True, "restartCount": 0}
                                ],
                            },
                        }
                    ],
                }
            else:
                body = {"apiVersion": "v1", "kind": "Status", "code": code, "message": "test status"}
            data = json.dumps(body).encode()
            try:
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                time.sleep(state.read_body_delay)
                self.wfile.write(data)
            except OSError:
                # A request timeout can close the socket before this reply.
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    allow_kubernetes_test_server(server)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    configuration = client.Configuration()
    configuration.host = f"http://127.0.0.1:{server.server_port}"
    configuration.retries = 3  # The polling client must not inherit these retries.
    configuration.api_key["authorization"] = "test-token"
    subject = KubeCtl.__new__(KubeCtl)
    with client.ApiClient(configuration, cookie="test-cookie") as original:
        original.default_headers["X-Cluster"] = "test-cluster"
        subject.core_v1_api = client.CoreV1Api(original)
        try:
            yield subject, state
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            original.rest_client.pool_manager.clear()


@pytest.mark.parametrize("method", ["wait_for_namespace_deletion", "wait_for_stable"])
def test_status_wait_preserves_authentication_and_retries_transient_api_errors(status_api, method):
    subject, state = status_api
    state.errors = [503]

    getattr(subject, method)("test", sleep=0, max_wait=2)

    assert len(state.requests) == 2
    for _path, headers in state.requests:
        assert headers["authorization"] == "test-token"
        assert headers["Cookie"] == "test-cookie"
        assert headers["X-Cluster"] == "test-cluster"
    assert subject.core_v1_api.api_client.configuration.retries == 3


@pytest.mark.parametrize("method", ["wait_for_namespace_deletion", "wait_for_stable"])
@pytest.mark.parametrize("delay_field", ["delay", "read_body_delay"])
def test_stalled_status_request_respects_a_fractional_remaining_budget(status_api, method, delay_field):
    subject, state = status_api
    setattr(state, delay_field, 0.6)
    started = time.monotonic()

    with pytest.raises(Exception, match="Timeout"):
        getattr(subject, method)("test", sleep=0.05, max_wait=0.2)

    assert time.monotonic() - started < 0.5
    assert state.requests


@pytest.mark.parametrize("method", ["wait_for_namespace_deletion", "wait_for_stable"])
def test_a_status_request_can_take_longer_than_ten_seconds(status_api, method):
    subject, state = status_api
    state.delay = 11
    started = time.monotonic()

    getattr(subject, method)("test", max_wait=15)

    assert 11 <= time.monotonic() - started < 15
    assert len(state.requests) == 1


@pytest.mark.parametrize("method", ["wait_for_namespace_deletion", "wait_for_stable"])
def test_refused_connection_is_retried_only_until_the_outer_deadline(status_api, method, allow_kubernetes_test_server):
    subject, _state = status_api
    # Bind a port without listening so connection refusal is deterministic.
    with contextlib.closing(socket.socket()) as closed_port:
        closed_port.bind(("127.0.0.1", 0))
        allow_kubernetes_test_server(closed_port)
        subject.core_v1_api.api_client.configuration.host = f"http://127.0.0.1:{closed_port.getsockname()[1]}"
        started = time.monotonic()
        with pytest.raises(Exception, match="Timeout"):
            getattr(subject, method)("test", sleep=0.05, max_wait=0.2)
        assert time.monotonic() - started < 0.5
