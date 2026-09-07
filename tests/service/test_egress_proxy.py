import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, PropertyMock

import pytest

from sregym.service import internet_policy


@pytest.fixture
def addon(monkeypatch):
    # The production image supplies mitmproxy. Keep host tests independent of
    # that image while exercising the addon's actual request hooks.
    monkeypatch.setitem(sys.modules, "internet_policy", internet_policy)
    monkeypatch.setitem(sys.modules, "mitmproxy", SimpleNamespace(ctx=Mock(), http=Mock()))
    path = Path(__file__).resolve().parents[2] / "docker" / "egress_proxy.py"
    spec = importlib.util.spec_from_file_location("egress_proxy_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "_block", Mock())
    monkeypatch.setattr(module, "_record_block", Mock())
    monkeypatch.setattr(module, "_load_policy", lambda: (internet_policy.EndpointRule("provider.test", 443),))
    return module


def flow(body):
    return SimpleNamespace(
        request=SimpleNamespace(
            method="POST",
            host="provider.test",
            port=443,
            scheme="https",
            path="/v1/messages",
            content=body,
            raw_content=b"compressed bytes are not JSON",
        )
    )


def test_request_inspects_decoded_content(addon):
    request_flow = flow(b'{"tools":[{"type":"web_search"}]}')
    addon.request(request_flow)
    addon._block.assert_called_once_with(request_flow, "provider-web-tool")


def test_audit_does_not_record_paths_queries_headers_or_bodies(addon):
    request_flow = flow(b"private body")
    request_flow.request.path = "/secret-in-path?token=secret-in-query#secret-fragment"
    request_flow.request.headers = {"Authorization": "Bearer secret-header"}
    record = addon._request_record(request_flow)
    assert set(record) == {"timestamp", "method", "scheme", "host", "port"}
    assert not any("secret" in str(value) or "private" in str(value) for value in record.values())


def test_invalid_content_encoding_fails_closed(addon):
    request_flow = flow(None)
    request_flow.request = Mock(method="POST", host="provider.test", port=443, path="/v1/messages")
    type(request_flow.request).content = PropertyMock(side_effect=ValueError("invalid gzip"))
    addon.request(request_flow)
    addon._block.assert_called_once_with(request_flow, "invalid-request-encoding")


def test_internal_application_data_is_not_a_provider_tool_declaration(addon, monkeypatch):
    monkeypatch.setattr(
        addon,
        "_load_policy",
        lambda: (internet_policy.EndpointRule("host.docker.internal", 8000, inspect_tools=False),),
    )
    request_flow = flow(b'{"tools":[{"type":"web_search"}],"apps":["catalog"]}')
    request_flow.request.host = "host.docker.internal"
    request_flow.request.port = 8000
    addon.request(request_flow)
    addon._block.assert_not_called()
    request_flow.request.port = 8001
    addon.request(request_flow)
    addon._block.assert_called_once_with(request_flow, "endpoint-not-allowed")


@pytest.mark.parametrize(
    "content_type,stream",
    [
        ("text/event-stream", True),
        ("Text/Event-Stream; charset=utf-8", True),
        ("application/json", False),
        ("", False),
    ],
)
def test_only_event_stream_responses_are_streamed(addon, content_type, stream):
    request_flow = flow(None)
    request_flow.response = SimpleNamespace(headers={"content-type": content_type}, stream=False)
    addon.responseheaders(request_flow)
    assert request_flow.response.stream is stream


@pytest.mark.parametrize(
    "from_client,tool,blocked", [(True, "web_search", True), (False, "web_search", False), (True, "function", False)]
)
def test_websocket_checks_client_requests_only(addon, from_client, tool, blocked):
    message = Mock(from_client=from_client, content='{"response":{"tools":[{"type":"' + tool + '"}]}}')
    request_flow = flow(None)
    request_flow.websocket = SimpleNamespace(messages=[message])
    request_flow.kill = Mock()
    addon.websocket_message(request_flow)
    assert message.drop.called is blocked
    request_flow.kill.assert_not_called()
    assert addon._record_block.called is blocked
    assert addon.ctx.master.commands.call.called is blocked
    if blocked:
        assert addon.ctx.master.commands.call.call_args.args[:3] == ("inject.websocket", request_flow, True)
