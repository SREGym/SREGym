"""start_port_forward accepts a forward only once it serves traffic.

Control flow over three seams (the probe, Popen and the clock), so none of
these needs a cluster.
"""

from unittest import mock

import pytest

from sregym.service import mcp_server
from sregym.service.mcp_server import MCPServer


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """start_port_forward exports MCP_SERVER_PORT and MCP_SERVER_URL on
    success. Start each test without them so the assertions below see only
    what the test itself did, and so nothing leaks into the next test."""
    monkeypatch.delenv("MCP_SERVER_PORT", raising=False)
    monkeypatch.delenv("MCP_SERVER_URL", raising=False)


def _server(healthy):
    """An MCPServer with the cluster and the network stubbed out."""
    s = MCPServer.__new__(MCPServer)
    s.namespace = "sregym"
    s.service_name = "mcp-server"
    s.port = 9954
    s.port_forward_process = None
    s.kubectl = mock.Mock()
    s._is_port_forward_healthy = mock.Mock(return_value=healthy)
    s._kill_stale_port_forward = mock.Mock()
    s.is_port_in_use = mock.Mock(return_value=False)
    s.stop_port_forward = mock.Mock()
    return s


def _fake_clock(monkeypatch):
    """A clock that only advances when the code under test sleeps.

    start_port_forward polls `while time.monotonic() < deadline`, so stubbing
    sleep alone turns a 20s deadline into a 20s busy-wait per attempt. Driving
    time from sleep keeps the deadline logic exercised and the test instant.
    """
    now = [1000.0]

    def sleep(seconds):
        now[0] += seconds

    monkeypatch.setattr(mcp_server.time, "sleep", sleep)
    monkeypatch.setattr(mcp_server.time, "monotonic", lambda: now[0])


def _alive_process(monkeypatch, exit_code=None):
    proc = mock.Mock()
    proc.poll.return_value = exit_code
    monkeypatch.setattr(mcp_server.subprocess, "Popen", mock.Mock(return_value=proc))
    return proc


def test_start_port_forward_leaves_a_serving_forward_alone():
    """It used to reclaim first and ask later, so a healthy forward could be
    torn down and, if the three retries then failed, replaced by nothing."""
    s = _server(healthy=True)

    MCPServer.start_port_forward(s)

    s._kill_stale_port_forward.assert_not_called()
    assert s.port_forward_process is None  # not ours to stop
    assert mcp_server.os.environ["MCP_SERVER_PORT"] == "9954"
    assert mcp_server.os.environ["MCP_SERVER_URL"] == "http://127.0.0.1:9954"


def test_start_port_forward_waits_for_traffic_before_accepting(monkeypatch):
    """A forward that binds but needs a moment to serve must not be destroyed.

    `poll() is None` said "healthy" the instant the process existed. Checking
    once three seconds in would tear down a forward that was about to work.
    """
    _fake_clock(monkeypatch)
    s = _server(healthy=False)
    s._is_port_forward_healthy = mock.Mock(side_effect=[False, False, True])
    _alive_process(monkeypatch)

    MCPServer.start_port_forward(s)

    assert s._is_port_forward_healthy.call_count == 3
    s.stop_port_forward.assert_not_called()
    assert mcp_server.os.environ["MCP_SERVER_PORT"] == "9954"


def test_start_port_forward_gives_up_when_the_forward_never_serves(monkeypatch):
    """A forward that binds and never answers is torn down, not accepted.

    Accepting one is what made every later tool call pay a full timeout
    against a socket that connects and never replies.
    """
    _fake_clock(monkeypatch)
    s = _server(healthy=False)
    _alive_process(monkeypatch)

    MCPServer.start_port_forward(s)

    assert s.stop_port_forward.call_count == 3  # one per attempt, then give up
    assert s._kill_stale_port_forward.call_count == 3  # a torn-down forward is swept before the next try
    assert s.port_forward_process is None  # nothing left for a caller to trust
    assert "MCP_SERVER_PORT" not in mcp_server.os.environ


def test_start_port_forward_stops_waiting_when_the_process_dies(monkeypatch):
    """Process death ends the wait early instead of burning the deadline."""
    _fake_clock(monkeypatch)
    s = _server(healthy=False)
    _alive_process(monkeypatch, exit_code=1)

    MCPServer.start_port_forward(s)

    # Only the early-return probe. Once poll() reports an exit there is
    # nothing left to probe, so the deadline is never waited out.
    assert s._is_port_forward_healthy.call_count == 1
    assert mcp_server.subprocess.Popen.call_count == 3  # each exit is retried
    assert s.stop_port_forward.call_count == 3
