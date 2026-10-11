from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from starlette.requests import Request

from mcp_server import kubectl_mcp_tools as tools


def context(headers=(), query=b"session_id=query"):
    return SimpleNamespace(
        request_context=SimpleNamespace(
            request=Request(
                {
                    "type": "http",
                    "method": "GET",
                    "scheme": "http",
                    "server": ("localhost", 9954),
                    "path": "/sse",
                    "query_string": query,
                    "headers": list(headers),
                }
            )
        )
    )


@pytest.mark.parametrize(
    "headers,query,expected",
    [
        ([(b"x-session-id", b"current")], b"session_id=query", "current"),
        ([(b"sregym_ssid", b"legacy")], b"session_id=query", "legacy"),
        ([(b"x-session-id", b"current"), (b"sregym_ssid", b"legacy")], b"session_id=query", "current"),
        ([], b"session_id=query", "query"),
        ([], b"session_id=alpha%2Bbeta%20gamma", "alpha+beta gamma"),
        ([], b"other=value", None),
        ([(b"x-session-id", b"")], b"session_id=query", ""),
        ([(b"sregym_ssid", b"")], b"session_id=query", ""),
    ],
)
def test_neutral_header_preserves_legacy_and_query_precedence(headers, query, expected):
    assert tools.extract_session_id(context(headers, query)) == expected


def test_all_session_transports_reuse_the_same_tool_and_other_sessions_stay_separate(monkeypatch):
    cache = {}
    monkeypatch.setattr(tools, "sessionCache", cache)
    factory = Mock(side_effect=lambda _: object())
    monkeypatch.setattr(tools, "KubectlToolSet", factory)
    requests = [
        context([(b"x-session-id", b"shared")]),
        context([(b"sregym_ssid", b"shared")]),
        context(query=b"session_id=shared"),
    ]
    resolved = [tools.get_tools(tools.extract_session_id(request)) for request in requests]
    assert resolved[0] is resolved[1] is resolved[2]
    assert tools.get_tools(tools.extract_session_id(context(query=b"session_id=other"))) is not resolved[0]
    assert factory.call_count == 2
    assert set(cache) == {"shared", "other"}
