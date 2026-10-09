from types import SimpleNamespace

import pytest

from mcp_server.kubectl_mcp_tools import extract_session_id


@pytest.mark.parametrize(
    "headers,expected",
    [
        ({"x-session-id": "current"}, "current"),
        ({"sregym_ssid": "legacy"}, "legacy"),
        ({"x-session-id": "current", "sregym_ssid": "legacy"}, "current"),
        ({}, "query"),
    ],
)
def test_session_header_migration_preserves_existing_session_and_query_fallback(headers, expected):
    ctx = SimpleNamespace(
        request_context=SimpleNamespace(
            request=SimpleNamespace(headers=headers, url="http://localhost/sse?session_id=query"),
        )
    )
    assert extract_session_id(ctx) == expected
