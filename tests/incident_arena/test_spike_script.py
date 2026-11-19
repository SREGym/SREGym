"""The load-spike sender must offer exactly RPS * seconds sends, however starved of CPU it is.

The old sender submitted one thread-pool job per request from a paced main loop. Under the toolbox's
500m CPU limit that loop was starved and sent ~50 requests instead of 1800, so the storm never latched.
"""

import ast
import socketserver
import subprocess
import sys
import threading

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
    def handle(self):
        head = self.rfile.readline()
        length = 0
        while (line := self.rfile.readline()) not in (b"\r\n", b""):
            if line.lower().startswith(b"content-length:"):
                length = int(line.split(b":")[1])
        self.rfile.read(length)
        if head.startswith(b"POST /messages HTTP/1.1"):
            with _Counter.lock:
                _Counter.posts += 1
        self.wfile.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}")


@pytest.mark.parametrize(("rps", "seconds"), [(100, 2), (40, 3)])
def test_every_scheduled_send_reaches_the_server(rps, seconds):
    _Counter.posts = 0
    with _Counter(("127.0.0.1", 0), _Handler) as server:
        threading.Thread(target=server.serve_forever, daemon=True).start()
        port = server.server_address[1]
        script = _SPIKE_SCRIPT.format(rps=rps, seconds=seconds).replace("'svc-message', 8000", f"'127.0.0.1', {port}")
        out = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=60, check=True)
        server.shutdown()
    assert out.stdout.strip() == f"spike sent {rps * seconds}"
    assert _Counter.posts == rps * seconds
