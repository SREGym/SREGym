"""The load-spike sender must offer exactly RPS * seconds sends, however starved of CPU it is.

The old sender submitted one thread-pool job per request from a paced main loop. Under the toolbox's
500m CPU limit that loop was starved and sent ~50 requests instead of 1800, so the storm never latched.
"""

import ast
import socket
import socketserver
import subprocess
import sys
import threading
import time

import pytest

from sregym.conductor.problems.apps.slack_spine.helpers import _SPIKE_SCRIPT
from sregym.conductor.problems.apps.slack_spine.retry_storm import LoadSpikeRPCRetryStormSlack

# Measured: the sender costs ~1.74 ms of CPU per send (3.13 CPU-s for 1800 sends). At the problem's
# rate that is a CPU demand of rate * cost, which must stay well under the toolbox's 0.5 CPU limit.
CPU_SECONDS_PER_SEND = 0.00174
TOOLBOX_CPU_LIMIT = 0.5


def test_default_spike_is_1800_sends_and_fits_the_toolbox_cpu_limit():
    problem = LoadSpikeRPCRetryStormSlack
    assert problem.SPIKE_RPS * problem.SPIKE_SECONDS == 1800
    demand = problem.SPIKE_RPS * CPU_SECONDS_PER_SEND
    assert demand < TOOLBOX_CPU_LIMIT / 4


@pytest.mark.parametrize(("rps", "seconds"), [(60, 30), (1, 1), (200, 2)])
def test_script_renders_to_valid_python(rps, seconds):
    ast.parse(_SPIKE_SCRIPT.format(rps=rps, seconds=seconds))


def test_script_uses_no_thread_pool():
    rendered = _SPIKE_SCRIPT.format(rps=60, seconds=30)
    assert "ThreadPoolExecutor" not in rendered
    assert "threading" not in rendered


class _Counter(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    posts = 0
    lock = threading.Lock()


class _Handler(socketserver.StreamRequestHandler):
    def read_message(self):
        head = self.rfile.readline()
        length = 0
        while (line := self.rfile.readline()) not in (b"\r\n", b""):
            if line.lower().startswith(b"content-length:"):
                length = int(line.split(b":")[1])
        self.rfile.read(length)
        if head.startswith(b"POST /messages HTTP/1.1"):
            with _Counter.lock:
                _Counter.posts += 1

    def handle(self):
        self.read_message()
        self.wfile.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}")


def _run_sender(port, rps, seconds):
    script = _SPIKE_SCRIPT.format(rps=rps, seconds=seconds).replace("'svc-message', 8000", f"'127.0.0.1', {port}")
    return subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=30)


@pytest.mark.parametrize(("rps", "seconds"), [(100, 2), (40, 3)])
def test_every_scheduled_send_reaches_the_server(rps, seconds):
    _Counter.posts = 0
    with _Counter(("127.0.0.1", 0), _Handler) as server:
        threading.Thread(target=server.serve_forever, daemon=True).start()
        port = server.server_address[1]
        out = _run_sender(port, rps, seconds)
        server.shutdown()
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == f"spike sent {rps * seconds} of {rps * seconds}"
    assert _Counter.posts == rps * seconds


def test_pending_connections_get_their_full_timeout():
    class SmallBacklogCounter(_Counter):
        request_queue_size = 1

    _Counter.posts = 0
    with SmallBacklogCounter(("127.0.0.1", 0), _Handler) as server:

        def serve_late():
            time.sleep(6.25)
            server.serve_forever()

        threading.Thread(target=serve_late, daemon=True).start()
        try:
            out = _run_sender(server.server_address[1], 10, 1)
            deadline = time.monotonic() + 2
            while _Counter.posts < 10 and time.monotonic() < deadline:
                time.sleep(0.01)
        finally:
            server.shutdown()

    assert out.returncode == 0, out.stderr
    assert _Counter.posts == 10
    assert out.stdout.strip() == "spike sent 10 of 10"


def test_connection_failure_reports_actual_writes_and_exits_unsuccessfully():
    # Keep the port bound without listening so no other process can accept it.
    with socket.socket() as endpoint:
        endpoint.bind(("127.0.0.1", 0))
        out = _run_sender(endpoint.getsockname()[1], 10, 1)

    assert out.returncode != 0
    assert out.stdout.strip() == "spike sent 0 of 10"
    assert "10 requests were not written" in out.stderr


def test_sender_does_not_wait_for_responses_after_all_writes():
    release = threading.Event()

    class NoResponseHandler(_Handler):
        def handle(self):
            self.read_message()
            release.wait(20)

    _Counter.posts = 0
    with _Counter(("127.0.0.1", 0), NoResponseHandler) as server:
        threading.Thread(target=server.serve_forever, daemon=True).start()
        started = time.monotonic()
        try:
            out = _run_sender(server.server_address[1], 10, 1)
            elapsed = time.monotonic() - started
        finally:
            release.set()
            server.shutdown()

    assert out.returncode == 0, out.stderr
    assert _Counter.posts == 10
    assert elapsed < 5
    assert out.stdout.strip() == "spike sent 10 of 10"
