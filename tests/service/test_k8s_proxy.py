import copy
import http.client
import json
import socket
import ssl
from datetime import timedelta

import pytest
from cryptography import x509

from sregym.service.k8s_proxy import (
    HELM_RELEASE_SECRET_NAME_PREFIX,
    HELM_RELEASE_SECRET_TYPE,
    KubernetesAPIProxy,
    _filter_resource_list,
    _inspect_workload_request,
    _is_helm_release_secret,
    _is_helm_release_secret_request,
    _is_hidden_namespace_request,
    _is_secret_collection_delete,
    _is_secret_watch_request,
    _requires_json_secret_response,
    _response_filter_type,
)

HELM_SECRET_NAME = f"{HELM_RELEASE_SECRET_NAME_PREFIX}astronomy-shop.v1"
HELM_SECRET = {
    "apiVersion": "v1",
    "kind": "Secret",
    "metadata": {
        "name": HELM_SECRET_NAME,
        "namespace": "astronomy-shop",
        "labels": {"owner": "helm"},
    },
    "type": HELM_RELEASE_SECRET_TYPE,
    "data": {"release": "pre-fault-manifest"},
}
ORDINARY_SECRET = {
    "apiVersion": "v1",
    "kind": "Secret",
    "metadata": {"name": "checkout-credentials", "namespace": "astronomy-shop"},
    "type": "Opaque",
    "data": {"password": "runtime-secret"},
}


class FakeResponse:
    def __init__(
        self,
        body: bytes,
        content_type: str = "application/json",
        status: int = 200,
        headers: list[tuple[str, str]] | None = None,
        fp=None,
    ):
        self.status = status
        self._body = body
        self._headers = headers or [("Content-Type", content_type), ("Content-Length", str(len(body)))]
        self.fp = fp

    def read(self) -> bytes:
        return self._body

    def getheader(self, name: str, default: str = "") -> str:
        return dict(self._headers).get(name, default)

    def getheaders(self) -> list[tuple[str, str]]:
        return self._headers

    def close(self):
        if self.fp is not None:
            self.fp.close()


class FakeHTTPSConnection:
    requests: list[tuple[str, str, dict[str, str]]] = []
    response = FakeResponse(b"{}")
    sock: socket.socket | None = None

    def __init__(self, *args, **kwargs):
        self.sock = type(self).sock

    def request(self, method: str, path: str, body=None, headers=None):
        self.requests.append((method, path, headers or {}))

    def getresponse(self) -> FakeResponse:
        return self.response

    def close(self):
        if self.sock is not None:
            self.sock.close()
            self.sock = None


@pytest.fixture
def proxy(monkeypatch):
    FakeHTTPSConnection.requests = []
    FakeHTTPSConnection.response = FakeResponse(b"{}")
    FakeHTTPSConnection.sock = None
    monkeypatch.setattr(http.client, "HTTPSConnection", FakeHTTPSConnection)

    instance = object.__new__(KubernetesAPIProxy)
    instance.hidden_namespaces = {"chaos-mesh", "khaos"}
    instance.hidden_labels = {"app": {"load-generator"}}
    instance.listen_port = 0
    instance.listen_host = "127.0.0.1"
    instance.block_workload_creation = False
    instance.server = None
    instance.server_thread = None
    instance._temp_files = []
    instance._bearer_token = None
    instance._agent_token = ""
    instance._agent_kubeconfig_path = None
    instance._server_cert_pem = None
    instance.ca_cert = None
    instance.client_cert = None
    instance.client_key = None
    instance.api_host = "kubernetes.example"
    instance.api_port = 443
    instance.start()
    try:
        yield instance
    finally:
        instance.stop()


def request(
    proxy: KubernetesAPIProxy,
    path: str,
    method: str = "GET",
    headers: dict | None = None,
    body: bytes | None = None,
):
    port = proxy.server.server_address[1]
    request_headers = dict(headers or {})
    request_headers.setdefault("Authorization", f"Bearer {proxy._agent_token}")
    request_headers.setdefault("Host", "127.0.0.1")
    request_headers.setdefault("Connection", "close")
    if body is not None:
        request_headers.setdefault("Content-Length", str(len(body)))

    request_head = f"{method} {path} HTTP/1.1\r\n" + "".join(
        f"{name}: {value}\r\n" for name, value in request_headers.items()
    )
    context = ssl._create_unverified_context()
    with (
        socket.create_connection(("127.0.0.1", port)) as raw_socket,
        context.wrap_socket(raw_socket, server_hostname="localhost") as connection,
    ):
        connection.sendall(request_head.encode() + b"\r\n" + (body or b""))
        response = http.client.HTTPResponse(connection)
        response.begin()
        result = response.status, response.headers, response.read()
    return result


def test_proxy_certificate_outlives_long_benchmark_campaigns(proxy):
    certificate = x509.load_pem_x509_certificate(proxy._server_cert_pem.encode())

    assert certificate.not_valid_after_utc - certificate.not_valid_before_utc >= timedelta(days=30)


def test_helm_release_detection_does_not_depend_on_labels():
    secret = copy.deepcopy(HELM_SECRET)
    secret["metadata"]["labels"] = {}

    assert _is_helm_release_secret(secret)
    assert not _is_helm_release_secret(ORDINARY_SECRET)


def test_helm_release_type_is_hidden_even_with_an_unexpected_name():
    secret = copy.deepcopy(HELM_SECRET)
    secret["metadata"]["name"] = "unexpected-name"

    assert _is_helm_release_secret(secret)


def test_resource_lists_hide_helm_records_but_keep_ordinary_secrets():
    result = _filter_resource_list(
        {"items": [copy.deepcopy(HELM_SECRET), copy.deepcopy(ORDINARY_SECRET)]},
        hidden_namespaces=set(),
        hidden_labels={},
    )

    assert result["items"] == [ORDINARY_SECRET]


def test_resource_lists_still_hide_configured_labels_and_namespaces():
    ordinary_pod = {"metadata": {"name": "frontend", "namespace": "astronomy-shop"}}
    load_generator = {
        "metadata": {"name": "load-generator", "namespace": "astronomy-shop", "labels": {"app": "load-generator"}}
    }
    chaos_pod = {"metadata": {"name": "chaos", "namespace": "chaos-mesh"}}

    result = _filter_resource_list(
        {"items": [ordinary_pod, load_generator, chaos_pod]},
        hidden_namespaces={"chaos-mesh"},
        hidden_labels={"app": {"load-generator"}},
    )

    assert result["items"] == [ordinary_pod]


def test_table_responses_hide_helm_records():
    result = _filter_resource_list(
        {"rows": [{"object": copy.deepcopy(HELM_SECRET)}, {"object": copy.deepcopy(ORDINARY_SECRET)}]},
        hidden_namespaces=set(),
        hidden_labels={},
    )

    assert result["rows"] == [{"object": ORDINARY_SECRET}]


def test_uninspectable_table_rows_fail_closed():
    result = _filter_resource_list(
        {"rows": [{"cells": [HELM_SECRET_NAME], "object": None}]},
        hidden_namespaces=set(),
        hidden_labels={},
    )

    assert result["rows"] == []


@pytest.mark.parametrize(
    "path",
    [
        f"/api/v1/namespaces/astronomy-shop/secrets/{HELM_SECRET_NAME}",
        f"/api/v1/namespaces/astronomy-shop/secrets/{HELM_SECRET_NAME}/status",
        f"/api/v1/%6eamespaces/astronomy-shop/secrets/{HELM_SECRET_NAME}",
        f"/api/v1/%256eamespaces/astronomy-shop/secrets/{HELM_SECRET_NAME}",
        f"/api/v1/namespaces/astronomy-shop/%73ecrets/{HELM_SECRET_NAME}",
    ],
)
def test_direct_helm_release_paths_are_recognized_after_decoding(path):
    assert _is_helm_release_secret_request(path)


def test_namespaced_and_encoded_lists_are_filtered():
    assert _response_filter_type("/api/v1/namespaces/astronomy-shop/secrets") == "resources"
    assert _response_filter_type("/api/v1/%6eamespaces/astronomy-shop/secrets") == "resources"
    assert _response_filter_type("/apis/apps/v1/namespaces/default/deployments") == "resources"


def test_encoded_hidden_namespace_path_is_blocked():
    path = "/api/v1/%6eamespaces/chaos-mesh/pods"
    assert _is_hidden_namespace_request(path, {"chaos-mesh"})


@pytest.mark.parametrize("watch_value", ["true", "TRUE", "1"])
def test_secret_watches_are_recognized(watch_value):
    path = f"/api/v1/namespaces/astronomy-shop/secrets?watch={watch_value}"
    assert _is_secret_watch_request(path)


def test_encoded_secret_watch_is_recognized():
    assert _is_secret_watch_request("/api/v1/namespaces/default/%73ecrets?w%61tch=true")


def test_only_secret_collection_deletes_are_blocked():
    path = "/api/v1/namespaces/astronomy-shop/secrets?labelSelector=owner%3Dhelm"
    assert _is_secret_collection_delete(path, "DELETE")
    assert not _is_secret_collection_delete(path, "GET")
    direct_path = f"/api/v1/namespaces/astronomy-shop/secrets/{HELM_SECRET_NAME}"
    assert not _is_secret_collection_delete(direct_path, "DELETE")


def test_secret_gets_require_json_but_other_methods_do_not():
    path = "/api/v1/namespaces/astronomy-shop/secrets"
    assert _requires_json_secret_response(path, "GET")
    assert not _requires_json_secret_response(path, "POST")


@pytest.mark.parametrize(
    ("content_type", "body"),
    [
        (
            "application/json",
            json.dumps({"spec": {"volumes": [{"secret": {"secretName": HELM_SECRET_NAME}}]}}).encode(),
        ),
        (
            "application/apply-patch+yaml",
            f"spec:\n  volumes:\n    - secret:\n        secretName: {HELM_SECRET_NAME}\n".encode(),
        ),
        (
            "application/json-patch+json",
            json.dumps([{"op": "add", "path": "/spec/volumes/0", "value": {"secretName": HELM_SECRET_NAME}}]).encode(),
        ),
    ],
)
def test_workload_secret_references_are_rejected(content_type, body):
    path = "/api/v1/namespaces/astronomy-shop/pods"
    assert _inspect_workload_request(path, "POST", body, content_type) == "forbidden"


def test_ordinary_workload_secret_reference_is_allowed():
    path = "/apis/apps/v1/namespaces/astronomy-shop/deployments/frontend"
    body = json.dumps({"spec": {"volumes": [{"secret": {"secretName": "checkout-credentials"}}]}}).encode()

    assert _inspect_workload_request(path, "PATCH", body, "application/strategic-merge-patch+json") is None


def test_uninspectable_workload_body_is_rejected():
    path = "/api/v1/namespaces/astronomy-shop/pods"
    assert _inspect_workload_request(path, "POST", b"protobuf", "application/vnd.kubernetes.protobuf") == "unsupported"


@pytest.mark.parametrize("method", ["GET", "PATCH", "PUT", "DELETE"])
def test_direct_helm_release_access_is_blocked_before_forwarding(proxy, method):
    status, _, _ = request(
        proxy,
        f"/api/v1/namespaces/astronomy-shop/secrets/{HELM_SECRET_NAME}",
        method=method,
    )

    assert status == 403
    assert FakeHTTPSConnection.requests == []


def test_secret_list_forces_json_and_filters_the_release(proxy):
    response = {"apiVersion": "v1", "kind": "SecretList", "items": [HELM_SECRET, ORDINARY_SECRET]}
    FakeHTTPSConnection.response = FakeResponse(json.dumps(response).encode())

    status, headers, body = request(
        proxy,
        "/api/v1/%6eamespaces/astronomy-shop/secrets",
        headers={"Accept": "application/vnd.kubernetes.protobuf"},
    )

    assert status == 200
    assert headers["Content-Type"] == "application/json"
    assert json.loads(body)["items"] == [ORDINARY_SECRET]
    assert FakeHTTPSConnection.requests[0][2]["Accept"] == "application/json"


def test_secret_list_fails_closed_if_upstream_returns_protobuf(proxy):
    FakeHTTPSConnection.response = FakeResponse(b"protobuf-data", "application/vnd.kubernetes.protobuf")

    status, _, body = request(proxy, "/api/v1/namespaces/astronomy-shop/secrets")

    assert status == 502
    assert b"protobuf-data" not in body


def test_secret_watch_is_blocked_before_forwarding(proxy):
    status, _, _ = request(proxy, "/api/v1/namespaces/astronomy-shop/secrets?watch=true")

    assert status == 403
    assert FakeHTTPSConnection.requests == []


def test_bulk_secret_delete_is_blocked_before_forwarding(proxy):
    status, _, _ = request(
        proxy,
        "/api/v1/namespaces/astronomy-shop/secrets?labelSelector=owner%3Dhelm",
        method="DELETE",
    )

    assert status == 403
    assert FakeHTTPSConnection.requests == []


def test_pod_cannot_mount_a_helm_release_secret(proxy):
    pod = {"spec": {"volumes": [{"secret": {"secretName": HELM_SECRET_NAME}}]}}
    status, _, _ = request(
        proxy,
        "/api/v1/namespaces/astronomy-shop/pods",
        method="POST",
        headers={"Content-Type": "application/json"},
        body=json.dumps(pod).encode(),
    )

    assert status == 403
    assert FakeHTTPSConnection.requests == []


def test_ordinary_secret_remains_accessible(proxy):
    FakeHTTPSConnection.response = FakeResponse(json.dumps(ORDINARY_SECRET).encode())

    status, _, body = request(proxy, "/api/v1/namespaces/astronomy-shop/secrets/checkout-credentials")

    assert status == 200
    assert json.loads(body) == ORDINARY_SECRET


@pytest.mark.parametrize("upgrade_protocol", ["websocket", "SPDY/3.1"])
def test_protocol_upgrade_relays_buffered_and_subsequent_bytes_in_both_directions(proxy, upgrade_protocol):
    proxy_side, upstream_side = socket.socketpair()
    proxy_side.settimeout(2)
    upstream_side.settimeout(2)
    upstream_reader = proxy_side.makefile("rb")
    FakeHTTPSConnection.sock = proxy_side
    FakeHTTPSConnection.response = FakeResponse(
        b"",
        status=101,
        headers=[
            ("Connection", "Upgrade"),
            ("Upgrade", upgrade_protocol),
            ("Sec-WebSocket-Accept", "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="),
            ("Sec-WebSocket-Protocol", "v5.channel.k8s.io"),
        ],
        fp=upstream_reader,
    )
    upstream_side.sendall(b"early server frame")
    assert upstream_reader.peek(18) == b"early server frame"

    port = proxy.server.server_address[1]
    context = ssl._create_unverified_context()
    try:
        with (
            socket.create_connection(("127.0.0.1", port), timeout=2) as raw_socket,
            context.wrap_socket(raw_socket, server_hostname="localhost") as connection,
        ):
            connection.settimeout(2)
            connection.sendall(
                b"GET /api/v1/namespaces/default/pods/example/exec?command=true&stdout=true HTTP/1.1\r\n"
                + f"Authorization: Bearer {proxy._agent_token}\r\n".encode()
                + b"Connection: Upgrade\r\n"
                + f"Upgrade: {upgrade_protocol}\r\n".encode()
                + b"Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
                + b"Sec-WebSocket-Version: 13\r\n"
                + b"Sec-WebSocket-Protocol: v5.channel.k8s.io\r\n"
                + b"X-Stream-Protocol-Version: v5.channel.k8s.io\r\n"
                + b"X-Stream-Protocol-Version: v4.channel.k8s.io\r\n"
                + b"\r\n"
                + b"early client frame"
            )
            response = http.client.HTTPResponse(connection)
            response.begin()

            assert response.status == 101
            assert response.version == 11
            assert response.getheader("Upgrade") == upgrade_protocol
            assert response.getheader("Sec-WebSocket-Accept") == "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="
            assert response.getheader("Sec-WebSocket-Protocol") == "v5.channel.k8s.io"
            assert response.getheader("Content-Length") is None
            assert upstream_side.recv(18) == b"early client frame"
            assert response.fp.read1(18) == b"early server frame"

            connection.sendall(b"client frame")
            assert upstream_side.recv(12) == b"client frame"

            upstream_side.sendall(b"server frame")
            assert response.fp.read1(12) == b"server frame"

            _, _, forwarded_headers = FakeHTTPSConnection.requests[0]
            assert forwarded_headers["Connection"] == "Upgrade"
            assert forwarded_headers["Upgrade"] == upgrade_protocol
            assert forwarded_headers["Sec-WebSocket-Key"] == "dGhlIHNhbXBsZSBub25jZQ=="
            assert forwarded_headers["Sec-WebSocket-Version"] == "13"
            assert forwarded_headers["Sec-WebSocket-Protocol"] == "v5.channel.k8s.io"
            assert forwarded_headers["X-Stream-Protocol-Version"] == "v5.channel.k8s.io, v4.channel.k8s.io"
    finally:
        upstream_side.close()
        upstream_reader.close()


def test_rejected_protocol_upgrade_uses_the_normal_response_path(proxy):
    response_body = b'{"kind":"Status","message":"pod not found"}'
    FakeHTTPSConnection.response = FakeResponse(response_body, status=404)

    status, headers, body = request(
        proxy,
        "/api/v1/namespaces/default/pods/missing/exec?command=true&stdout=true",
        method="POST",
        headers={"Connection": "Upgrade", "Upgrade": "websocket"},
    )

    assert status == 404
    assert headers["Content-Length"] == str(len(response_body))
    assert body == response_body
