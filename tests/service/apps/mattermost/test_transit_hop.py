"""The provider hop: which requests an impaired network slows, and what it counts as crossing."""

import asyncio

import pytest

from sregym.service.apps.mattermost.runtime import transit
from sregym.service.apps.mattermost.transit_cascade import transit_application

pytestmark = pytest.mark.usefixtures("offline_kubectl")


class Chunks:
    def __init__(self, *chunks):
        self.chunks = list(chunks)

    async def read(self, _):
        return self.chunks.pop(0) if self.chunks else b""


class Sink:
    def __init__(self):
        self.written = []

    def write(self, data):
        self.written.append(data)

    async def drain(self):
        return None

    def close(self):
        return None


@pytest.fixture
def impaired(monkeypatch):
    """An impaired link that records each delay it hands out instead of sleeping."""
    delays = []
    monkeypatch.setattr(transit.link, "waits", lambda: delays.append(0.8) or [0.8])

    async def no_sleep(_):
        return None

    monkeypatch.setattr(transit.asyncio, "sleep", no_sleep)
    monkeypatch.setitem(transit.counters, "requests", 0)
    return delays


def copy(mode, chunks):
    sink = Sink()
    asyncio.run(transit.pipe(Chunks(*chunks), sink, mode))
    return sink.written


def test_routes_name_their_mode():
    routes = transit.parse_routes("8065=mattermost.ns.svc:8065/request,9000=push.ns.svc:9000/stream,7000=a.svc:7000")
    assert routes == {
        8065: ("mattermost.ns.svc", 8065, "request"),
        9000: ("push.ns.svc", 9000, "stream"),
        7000: ("a.svc", 7000, None),
    }


def test_one_request_per_connection_is_not_slowed_twice(impaired):
    assert copy("request", [b"POST /api/v4/posts HTTP/1.0\r\n", b'{"message": "hi"}']) == [
        b"POST /api/v4/posts HTTP/1.0\r\n",
        b'{"message": "hi"}',
    ]
    assert impaired == []


def test_kept_alive_requests_after_the_first_are_slowed(impaired):
    assert copy("request", [b"GET /a", b"GET /b", b"POST /c", b"body"]) == [b"GET /a", b"GET /b", b"POST /c", b"body"]
    assert len(impaired) == 2


def test_an_unmarked_route_slows_only_the_connection(impaired):
    copy(None, [b"GET /a", b"GET /b"])
    assert impaired == []


def test_a_stream_route_slows_every_chunk(impaired):
    copy("stream", [b"a", b"b"])
    assert len(impaired) == 2


def test_requests_are_counted_only_on_request_routes(impaired):
    copy("request", [b"GET /a", b"GET /b", b"POST /c", b"body"])
    copy(None, [b"GET /d"])
    copy("stream", [b"POST /push"])
    assert transit.counters["requests"] == 3


def test_a_lost_request_resets_the_kept_alive_connection(impaired, monkeypatch):
    monkeypatch.setattr(transit.link, "waits", lambda: None)
    reset = []
    monkeypatch.setattr(transit, "reset", reset.append)
    sink = Sink()
    asyncio.run(transit.pipe(Chunks(b"GET /a", b"body", b"GET /b", b"GET /c"), sink, "request"))
    assert sink.written == [b"GET /a", b"body"]
    assert reset == [sink]


@pytest.mark.parametrize("tier", ["1", "1b", "2"])
def test_chat_crosses_the_provider_per_request_in_every_tier(tier):
    app = transit_application(tier)
    hop = next(
        d for d in app.provider_documents() if d.get("kind") == "Deployment" and d["metadata"]["name"] == "transit"
    )
    env = {e["name"]: e["value"] for e in hop["spec"]["template"]["spec"]["containers"][0]["env"]}
    chat = env["TRANSIT_ROUTES"].split(",")[0]
    assert chat.endswith(f"mattermost.{app.namespace}.svc:8065/request")
