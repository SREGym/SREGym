"""/healthz is a plain route on the MCP server, not one of the SSE mounts.

A GET on an SSE mount answers 200 and streams forever, so it cannot serve
as a liveness check. These tests pin /healthz as an immediate,
non-streaming response.
"""

from starlette.routing import Route
from starlette.testclient import TestClient

from mcp_server.sregym_mcp_server import app


def test_healthz_returns_200():
    client = TestClient(app)

    resp = client.get("/healthz")

    assert resp.status_code == 200
    assert resp.text == "ok"


def test_healthz_is_a_route_not_a_mount():
    """A Mount declared before /healthz would swallow it as a prefix match.
    Check that the exact path is registered as its own Route."""
    healthz_routes = [r for r in app.routes if isinstance(r, Route) and r.path == "/healthz"]

    assert len(healthz_routes) == 1


def test_healthz_content_type_is_not_event_stream():
    """Routing /healthz through one of the create_sse_app mounts would answer
    with text/event-stream and hold the connection open."""
    client = TestClient(app)

    resp = client.get("/healthz")

    assert resp.status_code == 200
    assert "text/event-stream" not in resp.headers.get("content-type", "")
