import base64
import copy
import http.client
import json
import os
import socket
import ssl
import threading
import time
import urllib.parse
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlsplit

import pytest
import yaml
from cryptography import x509
from kubernetes import config

from sregym.service.agent_visibility_policy import (
    HELM_RELEASE_SECRET_NAME_PREFIX,
    HELM_RELEASE_SECRET_TYPE,
    HIDDEN_LABELS,
    HIDDEN_NAMESPACES,
    MCP_CONTROL_NAMESPACE,
    VERIFIER_PROBE_NAMESPACE,
    filter_resource_list,
    is_helm_release_secret,
)
from sregym.service.k8s_proxy import (
    KubernetesAPIProxy,
    _inspect_workload_request,
    _is_cluster_egress_control_mutation,
    _is_filtered_object_read,
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


def test_explicit_upstream_config_overrides_ambient_incluster_credentials(monkeypatch, tmp_path):
    selected = tmp_path / "private-config"
    loaded = []

    def load(self, kubeconfig_path):
        loaded.append(kubeconfig_path)
        return "selected-api", 6443, "ca", "cert", "key"

    def unexpected_default():
        raise AssertionError("Explicit credentials must not select an ambient kubeconfig")

    exists = os.path.exists
    monkeypatch.setattr(
        os.path, "exists", lambda path: True if path == KubernetesAPIProxy._INCLUSTER_TOKEN_PATH else exists(path)
    )
    monkeypatch.setattr(KubernetesAPIProxy, "_load_cluster_config", load)
    monkeypatch.setattr(KubernetesAPIProxy, "_resolve_upstream_kubeconfig", staticmethod(unexpected_default))
    proxy = KubernetesAPIProxy(upstream_kubeconfig_path=str(selected))
    assert loaded == [str(selected)]
    assert (proxy.api_host, proxy.api_port, proxy.ca_cert) == ("selected-api", 6443, "ca")


@pytest.mark.parametrize("path", ["", "   "])
def test_empty_explicit_upstream_config_cannot_fall_back_to_ambient_credentials(path):
    with pytest.raises(ValueError, match="must not be empty"):
        KubernetesAPIProxy(upstream_kubeconfig_path=path)


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
    responses: dict[str, FakeResponse] = {}
    sock: socket.socket | None = None

    def __init__(self, *args, **kwargs):
        self.sock = type(self).sock

    def request(self, method: str, path: str, body=None, headers=None):
        if path in self.responses:
            # Object lookups use a separate connection from upgraded streams.
            self.sock = None
        self.path = path
        self.requests.append((method, path, headers or {}))

    def getresponse(self) -> FakeResponse:
        return self.responses.get(self.path, self.response)

    def close(self):
        if self.sock is not None:
            self.sock.close()
            self.sock = None


@pytest.fixture
def proxy(monkeypatch):
    FakeHTTPSConnection.requests = []
    FakeHTTPSConnection.response = FakeResponse(b"{}")
    FakeHTTPSConnection.responses = {}
    FakeHTTPSConnection.sock = None
    monkeypatch.setattr(http.client, "HTTPSConnection", FakeHTTPSConnection)

    instance = object.__new__(KubernetesAPIProxy)
    instance.hidden_namespaces = {"chaos-mesh", "khaos"}
    instance.hidden_labels = {"app": {"load-generator"}}
    instance.listen_port = 0
    instance.listen_host = "127.0.0.1"
    instance.restrict_network_access = False
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

    assert is_helm_release_secret(secret)
    assert not is_helm_release_secret(ORDINARY_SECRET)


@pytest.mark.parametrize("filtered", [False, True])
@pytest.mark.parametrize(
    "method,suffix,body",
    [
        ("GET", "", None),
        ("GET", "/log", None),
        ("POST", "/exec?command=sh", None),
        ("POST", "/attach", None),
        ("DELETE", "", None),
        ("PATCH", "", b'{"metadata":{"labels":{"operator.internal/role":null}}}'),
        ("PUT", "", b'{"metadata":{"name":"maintenance"},"spec":{"containers":[]}}'),
    ],
)
def test_private_node_helper_cannot_be_entered_read_deleted_or_relabelled(proxy, filtered, method, suffix, body):
    proxy.restrict_network_access = filtered
    # The fixture starts its handler before this setting changes; restart it.
    proxy.stop()
    proxy.start()
    path = "/api/v1/namespaces/astronomy-shop/pods/maintenance"
    helper = {
        "kind": "Pod",
        "metadata": {"name": "maintenance", "labels": {"operator.internal/role": "node-maintenance"}},
        "spec": {
            "containers": [{"name": "gc", "securityContext": {"privileged": True}}],
            "volumes": [{"name": "host", "hostPath": {"path": "/"}}],
        },
    }
    FakeHTTPSConnection.responses[path] = FakeResponse(json.dumps(helper).encode())
    status, _, _ = request(proxy, path + suffix, method, headers={"Content-Type": "application/json"}, body=body)
    assert status == 403
    assert FakeHTTPSConnection.requests
    assert all(
        upstream_method == "GET" and upstream_path == path
        for upstream_method, upstream_path, _ in FakeHTTPSConnection.requests
    )


@pytest.mark.parametrize("private", [False, True])
def test_collection_delete_cannot_remove_private_helpers_but_can_remove_application_pods(proxy, private):
    path = "/api/v1/namespaces/astronomy-shop/pods"
    pod = {"kind": "Pod", "metadata": {"name": "frontend"}}
    if private:
        pod["metadata"]["labels"] = {"operator.internal/role": "node-maintenance"}
    FakeHTTPSConnection.responses[path] = FakeResponse(json.dumps({"items": [pod]}).encode())
    status, _, _ = request(proxy, path, "DELETE")
    assert status == (403 if private else 200)
    assert any(method == "DELETE" for method, _, _ in FakeHTTPSConnection.requests) is (not private)


@pytest.mark.parametrize("method", ["GET", "PATCH", "DELETE"])
def test_ordinary_application_pod_remains_accessible(proxy, method):
    path = "/api/v1/namespaces/astronomy-shop/pods/frontend"
    FakeHTTPSConnection.responses[path] = FakeResponse(
        json.dumps({"kind": "Pod", "metadata": {"name": "frontend"}, "spec": {"containers": []}}).encode()
    )
    body = b'{"metadata":{"annotations":{"operator-note":"repaired"}}}' if method == "PATCH" else None
    status, _, _ = request(proxy, path, method, headers={"Content-Type": "application/json"}, body=body)
    assert status == 200
    assert any(
        upstream_method == method and upstream_path == path
        for upstream_method, upstream_path, _ in FakeHTTPSConnection.requests
    )


@pytest.mark.parametrize("secure", [False, True])
def test_workload_privilege_protection_is_independent_of_open_internet(proxy, secure):
    proxy.stop()
    proxy.protect_workload_boundary = secure
    proxy.start()
    pod = {"kind": "Pod", "spec": {"containers": [{"name": "app", "securityContext": {"privileged": True}}]}}
    status, _, _ = request(
        proxy,
        "/api/v1/namespaces/default/pods",
        "POST",
        headers={"Content-Type": "application/json"},
        body=json.dumps(pod).encode(),
    )
    assert proxy.restrict_network_access is False
    assert status == (403 if secure else 200)
    assert any(method == "POST" for method, _, _ in FakeHTTPSConnection.requests) is (not secure)


@pytest.mark.parametrize(
    "body,expected",
    [
        ({"spec": {"containers": [{"name": "app", "command": ["replacement"]}]}}, "network_escape"),
        ({"spec": {"hostNetwork": False}}, None),
    ],
)
def test_secure_patch_checks_merged_privileges_and_permits_removing_host_access(body, expected):
    current = {"kind": "Pod", "spec": {"hostNetwork": True, "containers": [{"name": "app"}]}}
    assert (
        _inspect_workload_request(
            "/api/v1/namespaces/default/pods/app",
            "PATCH",
            json.dumps(body).encode(),
            "application/merge-patch+json",
            current=current,
            restrict_network_access=False,
            protect_workload_boundary=True,
        )
        == expected
    )


@pytest.mark.parametrize("upstream_status", [403, 500])
def test_node_helper_policy_lookup_failure_cannot_forward_agent_mutation(proxy, upstream_status):
    path = "/api/v1/namespaces/default/pods/maintenance"
    FakeHTTPSConnection.responses[path] = FakeResponse(b"{}", status=upstream_status)
    status, _, _ = request(
        proxy, path, "PATCH", headers={"Content-Type": "application/json"}, body=b'{"metadata":{"labels":{}}}'
    )
    assert status == 502
    assert all(method == "GET" for method, _, _ in FakeHTTPSConnection.requests)


@pytest.mark.parametrize(
    "rootless,explicit_env,expected", [(False, False, False), (True, False, True), (False, True, True)]
)
def test_proxy_selects_workload_boundary_without_changing_internet_policy(
    monkeypatch, rootless, explicit_env, expected
):
    monkeypatch.delenv("SREGYM_ROOTLESS_WORKLOAD", raising=False)
    monkeypatch.delenv("PROTECT_WORKLOAD_BOUNDARY", raising=False)
    if rootless:
        monkeypatch.setenv("SREGYM_ROOTLESS_WORKLOAD", "1")
    if explicit_env:
        monkeypatch.setenv("PROTECT_WORKLOAD_BOUNDARY", "true")
    monkeypatch.setattr(KubernetesAPIProxy, "_INCLUSTER_TOKEN_PATH", "/nonexistent-synthetic-token")
    monkeypatch.setattr(KubernetesAPIProxy, "_resolve_upstream_kubeconfig", lambda self: "/synthetic-kubeconfig")
    monkeypatch.setattr(
        KubernetesAPIProxy, "_load_cluster_config", lambda self, **kwargs: ("kubernetes.example", 443, None, None, None)
    )
    instance = KubernetesAPIProxy(restrict_network_access=False)
    assert instance.protect_workload_boundary is expected
    assert instance.restrict_network_access is False


@pytest.mark.parametrize("content_type", ["application/strategic-merge-patch+json", "application/apply-patch+yaml"])
def test_secure_patch_cannot_hide_privilege_retained_in_named_container_lists(content_type):
    current = {
        "spec": {
            "template": {
                "spec": {
                    "containers": [
                        {"name": "app", "image": "old", "securityContext": {"privileged": True}},
                    ]
                }
            }
        }
    }
    replacement = {"spec": {"template": {"spec": {"containers": [{"name": "app", "image": "new"}]}}}}
    assert (
        _inspect_workload_request(
            "/apis/apps/v1/namespaces/default/deployments/app",
            "PATCH",
            json.dumps(replacement).encode(),
            content_type,
            current=current,
            restrict_network_access=False,
            protect_workload_boundary=True,
        )
        == "network_escape"
    )
    safe_current = {"spec": {"template": {"spec": {"containers": [{"name": "app", "image": "old"}]}}}}
    assert (
        _inspect_workload_request(
            "/apis/apps/v1/namespaces/default/deployments/app",
            "PATCH",
            json.dumps(replacement).encode(),
            content_type,
            current=safe_current,
            restrict_network_access=False,
            protect_workload_boundary=True,
        )
        is None
    )


@pytest.mark.parametrize("suffix", ["/exec", "/attach"])
def test_missing_pod_cannot_be_replaced_between_exec_inspection_and_forwarding(proxy, suffix):
    path = "/api/v1/namespaces/default/pods/missing"
    FakeHTTPSConnection.responses[path] = FakeResponse(b"{}", status=404)
    status, _, _ = request(proxy, path + suffix, "POST")
    assert status == 404
    assert [method for method, _, _ in FakeHTTPSConnection.requests] == ["GET"]


def test_reserved_node_maintenance_namespace_is_denied_without_upstream_lookup(proxy):
    status, _, _ = request(proxy, "/api/v1/namespaces/platform-maintenance/pods/future-helper/exec", "POST")
    assert status == 404
    assert FakeHTTPSConnection.requests == []


def test_collection_delete_adds_server_side_private_helper_exclusions(proxy):
    path = "/api/v1/namespaces/default/pods?labelSelector=app%3Dfrontend&dryRun=All"
    FakeHTTPSConnection.responses[path] = FakeResponse(b'{"items":[]}')
    status, _, _ = request(proxy, path, "DELETE")
    assert status == 200
    forwarded = [entry[1] for entry in FakeHTTPSConnection.requests if entry[0] == "DELETE"]
    assert len(forwarded) == 1
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(forwarded[0]).query)
    assert query["dryRun"] == ["All"]
    assert "app=frontend" in query["labelSelector"][0]
    assert "operator.internal/role notin (node-maintenance)" in query["labelSelector"][0]


@pytest.mark.parametrize("method,suffix", [("POST", "/exec?command=true"), ("PATCH", "")])
def test_protected_open_network_allows_ordinary_pod_exec_and_image_repair(proxy, method, suffix):
    proxy.stop()
    proxy.protect_workload_boundary = True
    proxy.start()
    path = "/api/v1/namespaces/default/pods/frontend"
    FakeHTTPSConnection.responses[path] = FakeResponse(
        json.dumps(
            {
                "apiVersion": "v1",
                "kind": "Pod",
                "metadata": {"name": "frontend", "namespace": "default"},
                "spec": {"containers": [{"name": "app", "image": "registry.example/app:v1"}]},
            }
        ).encode()
    )
    body = b'{"spec":{"containers":[{"name":"app","image":"registry.example/app:v2"}]}}' if method == "PATCH" else None
    status, _, _ = request(
        proxy,
        path + suffix,
        method,
        headers={"Content-Type": "application/strategic-merge-patch+json"},
        body=body,
    )
    assert status == 200
    assert any(
        upstream_method == method and upstream_path == path + suffix
        for upstream_method, upstream_path, _ in FakeHTTPSConnection.requests
    )


@pytest.mark.parametrize(
    "settings",
    [
        {"hostPID": True},
        {"hostIPC": True},
        {"hostNetwork": True},
        {"containers": [{"name": "app", "securityContext": {"privileged": True}}]},
        {"volumes": [{"name": "host", "hostPath": {"path": "/"}}]},
    ],
)
def test_protected_open_network_denies_exec_into_host_privileged_pod(proxy, settings):
    proxy.stop()
    proxy.protect_workload_boundary = True
    proxy.start()
    path = "/api/v1/namespaces/default/pods/unsafe"
    pod = {"kind": "Pod", "metadata": {"name": "unsafe"}, "spec": settings}
    FakeHTTPSConnection.responses[path] = FakeResponse(json.dumps(pod).encode())
    status, _, _ = request(proxy, path + "/exec", "POST")
    assert status == 403
    assert [method for method, _, _ in FakeHTTPSConnection.requests] == ["GET"]


def test_helm_release_type_is_hidden_even_with_an_unexpected_name():
    secret = copy.deepcopy(HELM_SECRET)
    secret["metadata"]["name"] = "unexpected-name"

    assert is_helm_release_secret(secret)


def test_resource_lists_hide_helm_records_but_keep_ordinary_secrets():
    result = filter_resource_list(
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

    result = filter_resource_list(
        {"items": [ordinary_pod, load_generator, chaos_pod]},
        hidden_namespaces={"chaos-mesh"},
        hidden_labels={"app": {"load-generator"}},
    )

    assert result["items"] == [ordinary_pod]


def test_chaos_control_plane_and_events_are_hidden_but_normal_events_remain():
    chaos_crd = {"kind": "CustomResourceDefinition", "metadata": {"name": "podchaos.chaos-mesh.org"}}
    chaos_event = {
        "kind": "Event",
        "metadata": {"name": "chaos-event", "namespace": "astronomy-shop"},
        "reportingComponent": "chaos-controller-manager",
    }
    network_chaos_event = {
        "metadata": {
            "name": "network-chaos-event",
            "namespace": "astronomy-shop",
            "annotations": {"chaos-mesh.org/type": "updated"},
        },
        "source": {"component": "podnetworkchaos"},
        "involvedObject": {"apiVersion": "chaos-mesh.org/v1alpha1", "kind": "PodNetworkChaos"},
    }
    kubelet_event = {
        "kind": "Event",
        "metadata": {"name": "pod-restarted", "namespace": "astronomy-shop"},
        "source": {"component": "kubelet"},
        "message": "Container restarted",
    }

    result = filter_resource_list(
        {"items": [chaos_crd, chaos_event, network_chaos_event, kubelet_event]}, {"chaos-mesh"}, {}
    )

    assert result["items"] == [kubelet_event]


def test_visible_pod_keeps_real_image_while_chaos_bookkeeping_is_removed():
    pod = {
        "kind": "Pod",
        "metadata": {
            "name": "checkout",
            "namespace": "astronomy-shop",
            "annotations": {"chaos-mesh.org/injected": "true", "team": "checkout"},
            "managedFields": [{"manager": "chaos-controller-manager"}, {"manager": "kubectl"}],
        },
        "spec": {"containers": [{"name": "checkout", "image": "registry.k8s.io/pause:3.9"}]},
    }

    result = filter_resource_list({"items": [pod]}, {"chaos-mesh"}, {})["items"][0]

    assert result["metadata"]["annotations"] == {"team": "checkout"}
    assert result["metadata"]["managedFields"] == [{"manager": "kubectl"}]
    assert result["spec"]["containers"][0]["image"] == "registry.k8s.io/pause:3.9"


def test_direct_objects_require_json_but_streaming_subresources_do_not():
    assert _is_filtered_object_read("/api/v1/namespaces/app/pods/frontend")
    assert _is_filtered_object_read("/api/v1/namespaces/app/pods/frontend/status")
    assert not _is_filtered_object_read("/api/v1/namespaces/app/pods/frontend/log")
    assert not _is_filtered_object_read("/api/v1/namespaces/app/pods/frontend/exec")


def test_direct_pod_read_sanitizes_metadata_without_changing_image(proxy):
    FakeHTTPSConnection.response = FakeResponse(
        json.dumps(
            {
                "kind": "Pod",
                "metadata": {
                    "name": "checkout",
                    "namespace": "astronomy-shop",
                    "annotations": {"chaos-mesh.org/injected": "true", "team": "checkout"},
                },
                "spec": {"containers": [{"image": "registry.k8s.io/pause:3.9"}]},
            }
        ).encode()
    )

    status, _, body = request(proxy, "/api/v1/namespaces/astronomy-shop/pods/checkout")
    pod = json.loads(body)

    assert status == 200
    assert pod["metadata"]["annotations"] == {"team": "checkout"}
    assert pod["spec"]["containers"][0]["image"] == "registry.k8s.io/pause:3.9"


def test_table_responses_hide_helm_records():
    result = filter_resource_list(
        {"rows": [{"object": copy.deepcopy(HELM_SECRET)}, {"object": copy.deepcopy(ORDINARY_SECRET)}]},
        hidden_namespaces=set(),
        hidden_labels={},
    )

    assert result["rows"] == [{"object": ORDINARY_SECRET}]


def test_table_responses_hide_chaos_objects_and_preserve_ordinary_rows():
    table = {
        "rows": [
            {"cells": ["podchaos.chaos-mesh.org"], "object": {"metadata": {"name": "podchaos.chaos-mesh.org"}}},
            {"cells": ["checkout"], "object": {"metadata": {"name": "checkout", "namespace": "app"}}},
        ]
    }

    result = filter_resource_list(table, {"chaos-mesh"}, {})

    assert result["rows"] == [{"cells": ["checkout"], "object": {"metadata": {"name": "checkout", "namespace": "app"}}}]


def test_uninspectable_table_rows_fail_closed():
    result = filter_resource_list(
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


def test_hidden_namespace_looks_absent_to_agent(proxy):
    status, _, body = request(proxy, "/api/v1/namespaces/chaos-mesh")

    assert status == 404
    assert b"chaos-mesh" not in body
    assert FakeHTTPSConnection.requests == []


def test_api_discovery_does_not_advertise_chaos_mesh(proxy):
    FakeHTTPSConnection.response = FakeResponse(
        json.dumps(
            {
                "kind": "APIGroupList",
                "groups": [{"name": "chaos-mesh.org"}, {"name": "apps"}],
            }
        ).encode()
    )

    status, _, body = request(proxy, "/apis")

    assert status == 200
    assert json.loads(body)["groups"] == [{"name": "apps"}]


@pytest.mark.parametrize(
    ("path", "payload", "expected"),
    [
        (
            "/openapi/v3",
            {"paths": {"apis/chaos-mesh.org/v1alpha1": {"serverRelativeURL": "hidden"}, "api/v1": {}}},
            {"paths": {"api/v1": {}}},
        ),
        (
            "/openapi/v2",
            {
                "definitions": {"org.chaos-mesh.v1alpha1.PodChaos": {}, "io.k8s.api.core.v1.Pod": {}},
                "paths": {"/apis/chaos-mesh.org/v1alpha1/podchaos": {}, "/api/v1/pods": {}},
            },
            {"definitions": {"io.k8s.api.core.v1.Pod": {}}, "paths": {"/api/v1/pods": {}}},
        ),
    ],
)
def test_openapi_discovery_hides_chaos_schemas(proxy, path, payload, expected):
    content_type = "text/plain; charset=utf-8" if path == "/openapi/v3" else "application/json"
    FakeHTTPSConnection.response = FakeResponse(json.dumps(payload).encode(), content_type=content_type)

    status, _, body = request(proxy, path)

    assert status == 200
    assert json.loads(body) == expected


@pytest.mark.parametrize(
    "path",
    [
        "/apis/chaos-mesh.org/v1alpha1",
        "/apis/%63haos-mesh.org/v1alpha1/namespaces/astronomy-shop/podchaos",
        "/openapi/v3/apis/chaos-mesh.org/v1alpha1",
        "/apis/apiextensions.k8s.io/v1/customresourcedefinitions/podchaos.chaos-mesh.org",
        "/apis/rbac.authorization.k8s.io/v1/clusterroles/chaos-mesh-controller-manager",
    ],
)
def test_direct_chaos_control_plane_requests_do_not_reach_upstream(proxy, path):
    status, _, body = request(proxy, path)

    assert status == 404
    assert b"chaos-mesh" not in body
    assert FakeHTTPSConnection.requests == []


def test_direct_chaos_event_looks_absent(proxy):
    FakeHTTPSConnection.response = FakeResponse(
        json.dumps(
            {
                "metadata": {"name": "network-event", "namespace": "astronomy-shop"},
                "involvedObject": {"apiVersion": "chaos-mesh.org/v1alpha1", "kind": "PodNetworkChaos"},
            }
        ).encode()
    )

    status, _, body = request(proxy, "/api/v1/namespaces/astronomy-shop/events/network-event")

    assert status == 404
    assert b"chaos-mesh" not in body


def test_direct_event_read_does_not_accept_uninspectable_protobuf(proxy):
    FakeHTTPSConnection.response = FakeResponse(b"opaque event", "application/vnd.kubernetes.protobuf")

    status, _, _ = request(
        proxy,
        "/api/v1/namespaces/astronomy-shop/events/network-event",
        headers={"Accept": "application/vnd.kubernetes.protobuf"},
    )

    assert status == 502
    assert FakeHTTPSConnection.requests[-1][2]["Accept"] == "application/json"


def test_upstream_error_does_not_expose_chaos_admission_component(proxy):
    FakeHTTPSConnection.response = FakeResponse(
        b'{"message":"chaos-mesh webhook denied this request"}',
        status=403,
    )

    status, headers, body = request(proxy, "/api/v1/namespaces/astronomy-shop/pods")

    assert status == 403
    assert headers["Content-Type"] == "application/json"
    assert b"chaos-mesh" not in body
    assert json.loads(body)["kind"] == "Status"


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
    "path",
    [
        "/apis/crd.projectcalico.org/v1/globalnetworkpolicies/adminnetworkpolicy.external-egress-boundary",
        "/apis/crd.projectcalico.org/v1/tiers/adminnetworkpolicy",
        "/apis/crd.projectcalico.org/v1/felixconfigurations/default",
        "/apis/policy.networking.k8s.io/v1alpha1/adminnetworkpolicies/early-allow",
        "/apis/apiextensions.k8s.io/v1/customresourcedefinitions/globalnetworkpolicies.crd.projectcalico.org",
    ],
)
def test_cluster_egress_controls_are_protected_from_mutation(path):
    assert _is_cluster_egress_control_mutation(path, "PATCH")
    assert not _is_cluster_egress_control_mutation(path, "GET")


def test_standard_kubernetes_network_policies_remain_mutable():
    path = "/apis/networking.k8s.io/v1/namespaces/hotel-reservation/networkpolicies/deny-all"

    assert not _is_cluster_egress_control_mutation(path, "PATCH")


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


@pytest.mark.parametrize(
    ("content_type", "body"),
    [
        (
            "application/strategic-merge-patch+json",
            json.dumps({"spec": {"template": {"spec": {"hostNetwork": True}}}}).encode(),
        ),
        (
            "application/json-patch+json",
            json.dumps([{"op": "add", "path": "/spec/template/spec/hostPID", "value": True}]).encode(),
        ),
        (
            "application/apply-patch+yaml",
            b"spec:\n  template:\n    spec:\n      containers:\n        - securityContext:\n            privileged: true\n",
        ),
        (
            "application/json",
            json.dumps({"spec": {"template": {"spec": {"volumes": [{"hostPath": {"path": "/"}}]}}}}).encode(),
        ),
    ],
)
def test_workload_network_escapes_are_rejected(content_type, body):
    path = "/apis/apps/v1/namespaces/astronomy-shop/deployments/frontend"

    current = {"spec": {"template": {"spec": {}}}}
    assert _inspect_workload_request(path, "PATCH", body, content_type, current=current) == "network_escape"


def test_safe_workload_patch_is_allowed():
    path = "/apis/apps/v1/namespaces/astronomy-shop/deployments/frontend"
    body = json.dumps({"spec": {"template": {"spec": {"containers": [{"name": "frontend", "image": "v2"}]}}}}).encode()

    assert _inspect_workload_request(path, "PATCH", body, "application/strategic-merge-patch+json") is None


@pytest.mark.parametrize("operation", ["copy", "move"])
def test_workload_patch_cannot_copy_a_new_host_network_permission(operation):
    path = "/apis/apps/v1/namespaces/demo/deployments/frontend"
    body = json.dumps(
        [
            {
                "op": operation,
                "from": "/spec/template/spec/automountServiceAccountToken",
                "path": "/spec/template/spec/hostNetwork",
            }
        ]
    ).encode()
    current = {"spec": {"template": {"spec": {"automountServiceAccountToken": True}}}}
    assert (
        _inspect_workload_request(path, "PATCH", body, "application/json-patch+json", current=current)
        == "network_escape"
    )


def test_workload_patch_can_copy_metadata():
    path = "/apis/apps/v1/namespaces/demo/deployments/frontend"
    body = b'[{"op":"copy","from":"/metadata/labels/app","path":"/metadata/labels/component"}]'
    assert (
        _inspect_workload_request(
            path, "PATCH", body, "application/json-patch+json", current={"metadata": {"labels": {"app": "frontend"}}}
        )
        is None
    )


def test_uninspectable_workload_body_is_rejected():
    path = "/api/v1/namespaces/astronomy-shop/pods"
    assert _inspect_workload_request(path, "POST", b"protobuf", "application/vnd.kubernetes.protobuf") == "unsupported"


@pytest.mark.parametrize(
    "resource,path",
    [
        ("Pod", "/api/v1/namespaces/default/pods"),
        ("Job", "/apis/batch/v1/namespaces/default/jobs"),
        ("Deployment", "/apis/apps/v1/namespaces/default/deployments"),
    ],
)
def test_filtered_proxy_allows_ordinary_workload_creation(proxy, resource, path):
    proxy.stop()
    proxy.restrict_network_access = True
    proxy.start()
    pod_spec = {"containers": [{"name": "probe", "image": "busybox"}]}
    spec = pod_spec if resource == "Pod" else {"template": {"spec": pod_spec}}
    status, _, _ = request(
        proxy,
        path,
        method="POST",
        headers={"Content-Type": "application/json"},
        body=json.dumps({"kind": resource, "metadata": {"name": "probe"}, "spec": spec}).encode(),
    )
    assert status == 200
    assert FakeHTTPSConnection.requests[-1][0] == "POST"


def test_filtered_proxy_accepts_unchanged_system_workload(proxy):
    proxy.stop()
    proxy.restrict_network_access = True
    proxy.start()
    current = {"kind": "DaemonSet", "spec": {"template": {"spec": {"hostNetwork": True}}}}
    FakeHTTPSConnection.response = FakeResponse(json.dumps(current).encode())
    status, _, _ = request(
        proxy,
        "/apis/apps/v1/namespaces/kube-system/daemonsets/calico-node",
        method="PUT",
        headers={"Content-Type": "application/json"},
        body=json.dumps(current).encode(),
    )
    assert status == 200
    assert [method for method, _, _ in FakeHTTPSConnection.requests] == ["GET", "PUT"]


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
@pytest.mark.parametrize(
    "old_tier,new_tier,expected",
    [
        ("default", "default", 200),
        ("default", "adminnetworkpolicy", 403),
        ("adminnetworkpolicy", "default", 403),
        ("default", "unknown", 403),
    ],
)
def test_filtered_calico_policy_writes_respect_tier_order(proxy, monkeypatch, method, old_tier, new_tier, expected):
    proxy.stop()
    proxy.restrict_network_access = True
    proxy.start()
    current = {"metadata": {"name": "default.internal"}, "spec": {"tier": old_tier}}
    proposed = {"metadata": {"name": "default.internal"}, "spec": {"tier": new_tier}}

    def getresponse(self):
        request_method, path, _ = self.requests[-1]
        if request_method == "GET" and "/tiers/" in path:
            order = {"default": 1_000_000, "adminnetworkpolicy": 1000}.get(path.rsplit("/", 1)[-1])
            return FakeResponse(json.dumps({"spec": {"order": order}}).encode())
        return FakeResponse(json.dumps(current if request_method == "GET" else proposed).encode())

    monkeypatch.setattr(FakeHTTPSConnection, "getresponse", getresponse)
    path = "/apis/crd.projectcalico.org/v1/namespaces/default/networkpolicies"
    if method != "POST":
        path += "/default.internal"
    if method == "POST":
        expected = 200 if new_tier == "default" else 403
    if method == "DELETE":
        expected = 200 if old_tier == "default" else 403
    status, _, _ = request(
        proxy,
        path,
        method=method,
        headers={"Content-Type": "application/merge-patch+json"},
        body=json.dumps(proposed).encode() if method != "DELETE" else None,
    )
    assert status == expected
    assert any(m == method for m, _, _ in FakeHTTPSConnection.requests) is (expected == 200)


def test_table_columns_are_preserved_and_hidden_rows_removed(proxy):
    table = {
        "kind": "Table",
        "columnDefinitions": [{"name": "Ready"}, {"name": "Restarts"}],
        "rows": [
            {"cells": ["1/1", 0], "object": {"metadata": {"name": "api"}}},
            {"cells": ["1/1", 2], "object": {"metadata": {"name": "load", "labels": {"app": "load-generator"}}}},
        ],
    }
    FakeHTTPSConnection.response = FakeResponse(json.dumps(table).encode())
    status, _, body = request(
        proxy,
        "/api/v1/pods?includeObject=None",
        headers={"Accept": "application/json;as=Table;v=v1;g=meta.k8s.io,application/json"},
    )
    result = json.loads(body)
    assert status == 200 and result["columnDefinitions"] == table["columnDefinitions"]
    assert len(result["rows"]) == 1 and result["rows"][0]["cells"] == ["1/1", 0]
    _, path, headers = FakeHTTPSConnection.requests[-1]
    assert "includeObject=Object" in path and "includeObject=None" not in path
    assert "as=Table" in headers["Accept"]


@pytest.mark.parametrize("watch", [False, True])
@pytest.mark.parametrize("filtered", [False, True])
def test_streams_arrive_before_upstream_closes_and_watch_hides_resources(proxy, watch, filtered):
    proxy.stop()
    proxy.restrict_network_access = filtered
    proxy.start()
    upstream_reader, upstream_writer = socket.socketpair()
    finish = threading.Event()
    visible = {
        "type": "ADDED",
        "object": {"metadata": {"name": "api", "annotations": {"chaos-mesh.org/injected": "true", "team": "api"}}},
    }
    expected_visible = {
        "type": "ADDED",
        "object": {"metadata": {"name": "api", "annotations": {"team": "api"}}},
    }
    hidden = {"type": "ADDED", "object": {"metadata": {"name": "load", "labels": {"app": "load-generator"}}}}
    first = (json.dumps(hidden) + "\n" + json.dumps(visible) + "\n").encode() if watch else b"first log line\n"
    content_type = "application/json" if watch else "text/plain"

    def produce():
        upstream_writer.sendall(
            f"HTTP/1.1 200 OK\r\nContent-Type: {content_type}\r\nTransfer-Encoding: chunked\r\n\r\n".encode()
        )
        upstream_writer.sendall(f"{len(first):x}\r\n".encode() + first + b"\r\n")
        finish.wait(3)
        upstream_writer.sendall(b"0\r\n\r\n")

    producer = threading.Thread(target=produce, daemon=True)
    producer.start()
    upstream_response = http.client.HTTPResponse(upstream_reader)
    upstream_response.begin()
    FakeHTTPSConnection.response = upstream_response
    FakeHTTPSConnection.responses["/api/v1/namespaces/default/pods/api"] = FakeResponse(
        json.dumps(
            {
                "apiVersion": "v1",
                "kind": "Pod",
                "metadata": {"name": "api", "namespace": "default"},
                "spec": {"containers": [{"name": "app", "image": "registry.example/app:v1"}]},
            }
        ).encode()
    )
    path = "/api/v1/pods?watch=true" if watch else "/api/v1/namespaces/default/pods/api/log?follow=true"
    try:
        with (
            socket.create_connection(("127.0.0.1", proxy.server.server_address[1]), timeout=1) as raw,
            ssl._create_unverified_context().wrap_socket(raw, server_hostname="localhost") as connection,
        ):
            connection.sendall(
                f"GET {path} HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer {proxy._agent_token}\r\nConnection: close\r\n\r\n".encode()
            )
            response = http.client.HTTPResponse(connection)
            response.begin()
            line = response.readline()
            assert response.status == 200 and not finish.is_set()
            assert (json.loads(line) == expected_visible) if watch else (line == first)
            finish.set()
            assert response.read() == b""
    finally:
        finish.set()
        producer.join(3)
        upstream_reader.close()
        upstream_writer.close()


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


@pytest.mark.parametrize("restricted", [False, True])
def test_host_network_restriction_does_not_change_open_mode(proxy, restricted):
    # The handler captures this flag when the server starts.
    proxy.stop()
    proxy.restrict_network_access = restricted
    proxy.start()
    patch = {"spec": {"template": {"spec": {"hostNetwork": True}}}}
    status, _, _ = request(
        proxy,
        "/apis/apps/v1/namespaces/astronomy-shop/deployments/frontend",
        method="PATCH",
        headers={"Content-Type": "application/strategic-merge-patch+json"},
        body=json.dumps(patch).encode(),
    )

    assert status == (403 if restricted else 200)
    assert any(method == "PATCH" for method, _, _ in FakeHTTPSConnection.requests) is not restricted


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/namespaces/default/pods/forged:80/proxy/",
        "/api/v1/namespaces/default/services/forged/proxy/",
        "/api/v1/nodes/forged/proxy/stats",
    ],
)
def test_filtered_proxy_blocks_status_relay_subresource(proxy, path):
    proxy.stop()
    proxy.restrict_network_access = True
    proxy.start()

    status, _, _ = request(proxy, path)

    assert status == 403
    assert FakeHTTPSConnection.requests == []


def test_open_proxy_allows_status_relay_subresource(proxy):
    status, _, _ = request(proxy, "/api/v1/namespaces/default/pods/forged:80/proxy/")

    assert status == 200
    assert FakeHTTPSConnection.requests


def test_filtered_proxy_blocks_exec_into_a_host_network_pod(proxy):
    proxy.stop()
    proxy.restrict_network_access = True
    proxy.start()
    FakeHTTPSConnection.response = FakeResponse(json.dumps({"spec": {"hostNetwork": True}}).encode())

    status, _, _ = request(proxy, "/api/v1/namespaces/kube-system/pods/calico-node-abcde/exec?command=sh")

    assert status == 403
    assert [(method, p) for method, p, _ in FakeHTTPSConnection.requests] == [
        ("GET", "/api/v1/namespaces/kube-system/pods/calico-node-abcde")
    ]


def test_filtered_proxy_allows_exec_into_an_ordinary_pod(proxy):
    proxy.stop()
    proxy.restrict_network_access = True
    proxy.start()
    FakeHTTPSConnection.response = FakeResponse(json.dumps({"spec": {"containers": []}}).encode())

    status, _, _ = request(proxy, "/api/v1/namespaces/astronomy-shop/pods/frontend-abcde/exec?command=sh")

    assert status == 200
    assert [method for method, _, _ in FakeHTTPSConnection.requests] == ["GET", "GET"]


def test_ordinary_secret_remains_accessible(proxy):
    FakeHTTPSConnection.response = FakeResponse(json.dumps(ORDINARY_SECRET).encode())

    status, _, body = request(proxy, "/api/v1/namespaces/astronomy-shop/secrets/checkout-credentials")

    assert status == 200
    assert json.loads(body) == ORDINARY_SECRET


@pytest.mark.parametrize("upgrade_protocol", ["websocket", "SPDY/3.1"])
def test_protocol_upgrade_relays_buffered_and_subsequent_bytes_in_both_directions(proxy, upgrade_protocol):
    FakeHTTPSConnection.responses["/api/v1/namespaces/default/pods/example"] = FakeResponse(
        b'{"kind":"Pod","metadata":{"name":"example","namespace":"default"},"spec":{"containers":[{"name":"app","image":"registry.example/app:v1"}]}}'
    )
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

            _, _, forwarded_headers = next(entry for entry in FakeHTTPSConnection.requests if "/exec?" in entry[1])
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
    FakeHTTPSConnection.responses["/api/v1/namespaces/default/pods/missing"] = FakeResponse(
        json.dumps(
            {
                "apiVersion": "v1",
                "kind": "Pod",
                "metadata": {"name": "missing", "namespace": "default"},
                "spec": {"containers": [{"name": "app", "image": "registry.example/app:v1"}]},
            }
        ).encode()
    )

    status, headers, body = request(
        proxy,
        "/api/v1/namespaces/default/pods/missing/exec?command=true&stdout=true",
        method="POST",
        headers={"Connection": "Upgrade", "Upgrade": "websocket"},
    )

    assert status == 404
    assert headers["Content-Length"] == str(len(response_body))
    assert body == response_body


def _connect_without_tls_handshake(proxy: KubernetesAPIProxy) -> socket.socket:
    """Open a TCP connection to the proxy and send no TLS data."""
    idle_client = socket.create_connection(("127.0.0.1", proxy.server.server_address[1]))
    # Give the accept loop time to take the connection.
    time.sleep(0.3)
    return idle_client


def test_stop_returns_while_a_client_has_not_finished_its_tls_handshake(proxy):
    idle_client = _connect_without_tls_handshake(proxy)
    stopper = threading.Thread(target=proxy.stop, daemon=True)
    try:
        stopper.start()
        stopper.join(timeout=5)
        stopped_in_time = not stopper.is_alive()
    finally:
        # Closing the idle connection unblocks an accept loop that waits for its handshake.
        idle_client.close()
        stopper.join(timeout=5)

    assert stopped_in_time


def test_requests_are_served_while_another_client_has_not_finished_its_tls_handshake(proxy):
    FakeHTTPSConnection.response = FakeResponse(b'{"kind":"PodList","items":[]}')
    statuses = []
    idle_client = _connect_without_tls_handshake(proxy)
    client = threading.Thread(
        target=lambda: statuses.append(request(proxy, "/api/v1/namespaces/default/pods")[0]),
        daemon=True,
    )
    try:
        client.start()
        client.join(timeout=5)
        served_in_time = not client.is_alive()
    finally:
        idle_client.close()
        client.join(timeout=5)

    assert served_in_time
    assert statuses == [200]


def test_events_for_hidden_workloads_are_filtered_from_lists_and_direct_reads(proxy):
    loadgen_event = {
        "metadata": {"name": "load-generator-5d945c566-2lbl4.18a1", "namespace": "astronomy-shop"},
        "involvedObject": {
            "apiVersion": "v1",
            "kind": "Pod",
            "name": "wrk2-job-check",
            "namespace": "astronomy-shop",
        },
        "reason": "Scheduled",
        "message": "Successfully assigned astronomy-shop/load-generator-5d945c566-2lbl4 to worker",
    }
    frontend_event = {
        "metadata": {"name": "frontend-abc.18a1", "namespace": "astronomy-shop"},
        "involvedObject": {
            "apiVersion": "v1",
            "kind": "Pod",
            "name": "frontend-abc",
            "namespace": "astronomy-shop",
        },
        "reason": "Scheduled",
        "message": "Successfully assigned astronomy-shop/frontend-abc to worker",
    }
    proxy.hidden_labels["job"] = {"workload"}
    FakeHTTPSConnection.responses["/api/v1/namespaces/astronomy-shop/pods/wrk2-job-check"] = FakeResponse(
        json.dumps({"metadata": {"name": "wrk2-job-check", "labels": {"job": "workload"}}}).encode()
    )
    FakeHTTPSConnection.responses["/api/v1/namespaces/astronomy-shop/pods/frontend-abc"] = FakeResponse(b"{}")
    FakeHTTPSConnection.response = FakeResponse(json.dumps({"items": [loadgen_event, frontend_event]}).encode())
    status, _, body = request(proxy, "/api/v1/namespaces/astronomy-shop/events")
    assert status == 200
    assert json.loads(body)["items"] == [frontend_event]
    FakeHTTPSConnection.response = FakeResponse(json.dumps(loadgen_event).encode())
    status, _, _ = request(proxy, "/api/v1/namespaces/astronomy-shop/events/load-generator-5d945c566-2lbl4.18a1")
    assert status == 404


@pytest.mark.parametrize("reference_field", ["involvedObject", "regarding"])
@pytest.mark.parametrize("format", ["list", "table", "direct", "direct-table"])
def test_event_visibility_uses_labels_not_name_prefixes(proxy, reference_field, format):
    pod = {
        "kind": "Pod",
        "metadata": {"name": "load-generator-diagnostic", "uid": "pod-1", "labels": {"app": "diagnostic"}},
    }
    event = {
        "kind": "Event",
        "metadata": {"name": "check", "namespace": "app"},
        reference_field: {
            "apiVersion": "v1",
            "kind": "Pod",
            "name": "load-generator-diagnostic",
            "namespace": "app",
            "uid": "pod-1",
        },
    }
    FakeHTTPSConnection.responses["/api/v1/namespaces/app/pods/load-generator-diagnostic"] = FakeResponse(
        json.dumps(pod).encode()
    )
    payload = (
        {"items": [event]}
        if format == "list"
        else {"kind": "Table", "rows": [{"object": event}]}
        if format in {"table", "direct-table"}
        else event
    )
    FakeHTTPSConnection.response = FakeResponse(json.dumps(payload).encode())
    path = "/api/v1/namespaces/app/events" + ("/check" if format.startswith("direct") else "")
    status, _, body = request(proxy, path)
    assert status == 200
    assert json.loads(body) == payload


def test_deleted_event_reference_uses_uid_and_live_label_changes_override_cache(proxy):
    pod_path = "/api/v1/namespaces/app/pods/worker"
    event = {
        "kind": "Event",
        "metadata": {"name": "check", "namespace": "app"},
        "involvedObject": {"apiVersion": "v1", "kind": "Pod", "name": "worker", "namespace": "app", "uid": "old"},
    }
    pod = {
        "kind": "Pod",
        "metadata": {"name": "worker", "namespace": "app", "uid": "old", "labels": {"app": "load-generator"}},
    }
    FakeHTTPSConnection.responses[pod_path] = FakeResponse(json.dumps(pod).encode())
    FakeHTTPSConnection.response = FakeResponse(json.dumps(event).encode())
    assert request(proxy, "/api/v1/namespaces/app/events/check")[0] == 404
    FakeHTTPSConnection.responses[pod_path] = FakeResponse(b"{}", status=404)
    assert request(proxy, "/api/v1/namespaces/app/events/check")[0] == 404
    pod["metadata"]["uid"] = "new"
    pod["metadata"]["labels"] = {"app": "diagnostic"}
    FakeHTTPSConnection.responses[pod_path] = FakeResponse(json.dumps(pod).encode())
    assert request(proxy, "/api/v1/namespaces/app/events/check")[0] == 404
    event["involvedObject"]["uid"] = "new"
    FakeHTTPSConnection.response = FakeResponse(json.dumps(event).encode())
    assert request(proxy, "/api/v1/namespaces/app/events/check")[0] == 200
    pod["metadata"]["labels"] = {"app": "load-generator"}
    FakeHTTPSConnection.responses[pod_path] = FakeResponse(json.dumps(pod).encode())
    assert request(proxy, "/api/v1/namespaces/app/events/check")[0] == 404
    pod["metadata"]["labels"] = {"app": "diagnostic"}
    FakeHTTPSConnection.responses[pod_path] = FakeResponse(json.dumps(pod).encode())
    assert request(proxy, "/api/v1/namespaces/app/events/check")[0] == 200


def test_event_reference_lookup_error_fails_closed(proxy):
    event = {"involvedObject": {"apiVersion": "v1", "kind": "Pod", "name": "worker", "namespace": "app"}}
    FakeHTTPSConnection.responses["/api/v1/namespaces/app/pods/worker"] = FakeResponse(b"{}", status=503)
    FakeHTTPSConnection.response = FakeResponse(json.dumps({"items": [event]}).encode())
    assert request(proxy, "/api/v1/namespaces/app/events")[0] == 502


@pytest.mark.parametrize("resource", ["pods", "events"])
def test_direct_table_reads_do_not_expose_hidden_objects(proxy, resource):
    pod = {"kind": "Pod", "metadata": {"name": "worker", "namespace": "app", "labels": {"app": "load-generator"}}}
    event = {"involvedObject": {"apiVersion": "v1", "kind": "Pod", "name": "worker", "namespace": "app"}}
    FakeHTTPSConnection.responses["/api/v1/namespaces/app/pods/worker"] = FakeResponse(json.dumps(pod).encode())
    obj = pod if resource == "pods" else event
    FakeHTTPSConnection.response = FakeResponse(json.dumps({"kind": "Table", "rows": [{"object": obj}]}).encode())
    assert request(proxy, f"/api/v1/namespaces/app/{resource}/check")[0] == 404


def test_unknown_deleted_reference_does_not_guess_from_name(proxy):
    event = {
        "involvedObject": {
            "apiVersion": "v1",
            "kind": "Pod",
            "name": "load-generator-diagnostic",
            "namespace": "app",
            "uid": "unseen",
        }
    }
    FakeHTTPSConnection.responses["/api/v1/namespaces/app/pods/load-generator-diagnostic"] = FakeResponse(
        b"{}", status=404
    )
    FakeHTTPSConnection.response = FakeResponse(json.dumps(event).encode())
    assert request(proxy, "/api/v1/namespaces/app/events/check")[0] == 200


def test_resolve_upstream_kubeconfig_honors_env_and_skips_agent_proxy_files(monkeypatch, tmp_path):
    agent_cfg = tmp_path / "sregym-agent-kubeconfig-12345.yaml"
    agent_cfg.write_text("apiVersion: v1\n")
    cluster_cfg = tmp_path / "isolated-cluster-kubeconfig"
    cluster_cfg.write_text("apiVersion: v1\n")

    monkeypatch.setenv("KUBECONFIG", f"{agent_cfg}:{cluster_cfg}")
    assert KubernetesAPIProxy._resolve_upstream_kubeconfig() == str(cluster_cfg)


def test_explicit_missing_kubeconfig_does_not_select_another_cluster(monkeypatch, tmp_path):
    missing = tmp_path / "missing.yaml"
    monkeypatch.setenv("KUBECONFIG", str(missing))
    assert KubernetesAPIProxy._resolve_upstream_kubeconfig() == str(missing)
    with pytest.raises(config.ConfigException):
        KubernetesAPIProxy()


def test_upstream_kubeconfig_merges_files_and_resolves_relative_certificates(monkeypatch, tmp_path):
    import yaml

    context = tmp_path / "context.yaml"
    credentials_dir = tmp_path / "credentials"
    credentials_dir.mkdir()
    credentials = credentials_dir / "cluster.yaml"
    (credentials_dir / "ca.pem").write_text("test-ca")
    (credentials_dir / "cert.pem").write_text("test-cert")
    (credentials_dir / "key.pem").write_text("test-key")
    context.write_text(
        yaml.safe_dump(
            {"current-context": "real", "contexts": [{"name": "real", "context": {"cluster": "real", "user": "real"}}]}
        )
    )
    credentials.write_text(
        yaml.safe_dump(
            {
                "clusters": [
                    {
                        "name": "real",
                        "cluster": {"server": "https://real-cluster:7443", "certificate-authority": "ca.pem"},
                    }
                ],
                "users": [
                    {
                        "name": "real",
                        "user": {"client-certificate": "cert.pem", "client-key": "key.pem", "token": "test-token"},
                    }
                ],
            }
        )
    )
    monkeypatch.setenv("KUBECONFIG", f"{context}:{credentials}")
    proxy = KubernetesAPIProxy()
    assert proxy.api_host == "real-cluster"
    assert proxy.api_port == 7443
    assert (proxy.ca_cert, proxy.client_cert, proxy.client_key) == ("test-ca", "test-cert", "test-key")
    assert proxy._bearer_token == "test-token"


@pytest.mark.parametrize("namespace", [MCP_CONTROL_NAMESPACE, VERIFIER_PROBE_NAMESPACE])
@pytest.mark.parametrize(
    "method,suffix",
    [("GET", "/pods"), ("GET", "/pods/control/log"), ("POST", "/pods/control/exec"), ("DELETE", "/pods/control")],
)
def test_control_services_cannot_be_read_executed_or_deleted_by_agent(proxy, namespace, method, suffix):
    proxy.hidden_namespaces = HIDDEN_NAMESPACES.copy()
    # The handler captures the policy at startup, as the actual server does.
    proxy.stop()
    proxy.start()
    status, _, body = request(proxy, f"/api/v1/namespaces/{namespace}{suffix}", method=method)
    assert status == 404
    assert namespace.encode() not in body
    assert FakeHTTPSConnection.requests == []


def test_control_bindings_are_private_without_changing_application_state():
    binding = {
        "metadata": {"name": "control-access"},
        "roleRef": {"kind": "ClusterRole", "name": "mcp-server"},
        "subjects": [{"kind": "ServiceAccount", "name": "mcp-server", "namespace": MCP_CONTROL_NAMESPACE}],
    }
    application = {
        "metadata": {"name": "frontend", "namespace": "hotel-reservation"},
        "spec": {"containers": [{"image": "registry.example/app:v1"}]},
    }
    result = filter_resource_list({"items": [binding, copy.deepcopy(application)]}, HIDDEN_NAMESPACES, HIDDEN_LABELS)
    assert result["items"] == [application]


def test_upstream_exception_does_not_leak_private_runner_paths(proxy, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("private runner /repo/SREGym/private-verifier-state")

    monkeypatch.setattr(FakeHTTPSConnection, "request", fail)
    status, _, body = request(proxy, "/api/v1/nodes")
    assert status == 502
    assert b"Upstream request failed" in body
    assert b"SREGym" not in body and b"private-verifier-state" not in body


@pytest.mark.parametrize("blocked", [None, "secret", "network"])
@pytest.mark.parametrize("method", ["POST", "PUT"])
def test_binary_workload_is_decoded_without_persisting_before_policy_check(proxy, monkeypatch, blocked, method):
    proxy.stop()
    proxy.restrict_network_access = True
    proxy.start()
    spec = {"containers": [{"name": "web", "image": "nginx"}]}
    if blocked == "secret":
        spec["imagePullSecrets"] = [{"name": HELM_SECRET_NAME}]
    elif blocked == "network":
        spec["hostNetwork"] = True
    decoded = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": "web", "namespace": "demo", "uid": "dry-run-uid", "resourceVersion": "7"},
        "spec": {"template": {"spec": spec}},
    }
    calls = []

    def record_request(self, request_method, path, body=None, headers=None):
        calls.append((request_method, path, body, headers or {}))

    def getresponse(self):
        if calls[-1][0] == "GET":
            return FakeResponse(b'{"spec":{"template":{"spec":{"containers":[{"name":"web","image":"nginx"}]}}}}')
        return FakeResponse(json.dumps(decoded).encode())

    monkeypatch.setattr(FakeHTTPSConnection, "request", record_request)
    monkeypatch.setattr(FakeHTTPSConnection, "getresponse", getresponse)
    path = "/apis/apps/v1/namespaces/demo/deployments" + ("/web" if method == "PUT" else "")
    status, _, _ = request(
        proxy,
        path + "?fieldManager=operator",
        method=method,
        headers={"Content-Type": "application/vnd.kubernetes.protobuf"},
        body=b"k8s\x00public-fixture",
    )
    dry_call = calls[0]
    assert dry_call[0] == method
    assert dry_call[1] == path + "?fieldManager=operator&dryRun=All"
    assert dry_call[2] == b"k8s\x00public-fixture"
    assert dry_call[3]["Accept"] == "application/json"
    real_calls = [call for call in calls if call[0] == method and call[1] == path + "?fieldManager=operator"]
    if blocked:
        assert status == 403
        assert not real_calls
    else:
        assert status == 200
        assert len(real_calls) == 1
        _, _, body, headers = real_calls[0]
        assert headers["Content-Type"] == "application/json"
        assert "Content-Length" not in headers
        forwarded = json.loads(body)
        assert ("uid" in forwarded["metadata"]) is (method == "PUT")
        assert ("resourceVersion" in forwarded["metadata"]) is (method == "PUT")


@pytest.mark.parametrize(
    "payload,content_type,status",
    [
        (b"not JSON", "application/json", 200),
        (b"{}", "application/json", 200),
        (b"[]", "application/json", 200),
        (b"binary", "application/vnd.kubernetes.protobuf", 200),
        (b'{"spec":{},"metadata":{}}', "application/json", 422),
    ],
)
def test_binary_workload_decoder_failure_never_submits_real_mutation(proxy, payload, content_type, status):
    FakeHTTPSConnection.response = FakeResponse(payload, content_type=content_type, status=status)
    result, _, _ = request(
        proxy,
        "/apis/apps/v1/namespaces/demo/deployments",
        method="POST",
        headers={"Content-Type": "application/vnd.kubernetes.protobuf"},
        body=b"k8s\x00public-fixture",
    )
    assert result == 422
    assert len(FakeHTTPSConnection.requests) == 1
    assert FakeHTTPSConnection.requests[0][1].endswith("?dryRun=All")


@pytest.mark.parametrize("status", [403, 409, 422, 429, 500])
def test_binary_dry_run_rejection_preserves_status_without_private_body(proxy, status):
    FakeHTTPSConnection.response = FakeResponse(
        b'{"kind":"Status","message":"system:serviceaccount:sregym:mcp-server private detail"}', status=status
    )
    result, _, body = request(
        proxy,
        "/api/v1/namespaces/demo/pods/pod",
        method="PUT",
        headers={"Content-Type": "application/vnd.kubernetes.protobuf"},
        body=b"k8s\x00public-fixture",
    )
    assert result == status
    assert b"sregym" not in body and b"private detail" not in body
    assert len(FakeHTTPSConnection.requests) == 1
    assert FakeHTTPSConnection.requests[0][1].endswith("?dryRun=All")


@pytest.fixture
def control_proxy(proxy):
    proxy.stop()
    proxy.hidden_namespaces = HIDDEN_NAMESPACES.copy()
    proxy.hidden_labels = HIDDEN_LABELS.copy()
    proxy.start()
    return proxy


@pytest.mark.parametrize("method", ["POST", "PUT"])
@pytest.mark.parametrize("manual", [False, True])
@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("root_mode", ["absent", "generated", "explicit"])
def test_binary_job_preserves_user_labels_and_manual_selectors(proxy, monkeypatch, method, manual, dry_run, root_mode):
    labels = {
        "team": "blue",
        "controller-uid": "temporary-uid",
        "batch.kubernetes.io/controller-uid": "temporary-uid",
        "job-name": "job",
        "batch.kubernetes.io/job-name": "job",
    }
    decoded = {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": "job", "namespace": "demo", "uid": "temporary-uid", "resourceVersion": "7"},
        "spec": {
            "manualSelector": manual,
            "selector": {
                "matchLabels": dict(labels) if manual else {"batch.kubernetes.io/controller-uid": "temporary-uid"}
            },
            "template": {
                "metadata": {"labels": dict(labels)},
                "spec": {"restartPolicy": "Never", "containers": [{"name": "work", "image": "busybox"}]},
            },
        },
    }
    root_labels = (
        dict(labels) if root_mode == "generated" else {"team": "operators", "controller-uid": "operator-value"}
    )
    if root_mode != "absent":
        decoded["metadata"]["labels"] = dict(root_labels)
    calls = []

    def record_request(self, request_method, path, body=None, headers=None):
        self.path = path
        calls.append((request_method, path, body))

    monkeypatch.setattr(FakeHTTPSConnection, "request", record_request)
    FakeHTTPSConnection.response = FakeResponse(json.dumps(decoded).encode())
    path = "/apis/batch/v1/namespaces/demo/jobs" + ("/job" if method == "PUT" else "")
    if dry_run:
        path += "?dryRun=All"
    status, _, _ = request(
        proxy,
        path,
        method=method,
        body=b"k8s\x00public-job-fixture",
        headers={"Content-Type": "application/vnd.kubernetes.protobuf"},
    )
    assert status == 200
    forwarded = json.loads(calls[-1][2])
    template = forwarded["spec"]["template"]["metadata"]["labels"]
    selector = forwarded["spec"]["selector"]["matchLabels"]
    assert template["team"] == "blue"
    if manual:
        assert selector["team"] == "blue"
    else:
        assert "team" not in selector
    for label in ("controller-uid", "batch.kubernetes.io/controller-uid"):
        if method == "POST" and not manual:
            assert label not in template and label not in selector
        else:
            assert template[label] == "temporary-uid"
            if manual or label == "batch.kubernetes.io/controller-uid":
                assert selector[label] == "temporary-uid"
    assert ("uid" in forwarded["metadata"]) is (method == "PUT")
    if root_mode == "absent" or (root_mode == "generated" and method == "POST" and not manual):
        assert "labels" not in forwarded["metadata"]
    else:
        assert forwarded["metadata"]["labels"] == root_labels
    assert labels["controller-uid"] == "temporary-uid"


@pytest.mark.parametrize("resource", ["clusterroles", "clusterrolebindings"])
@pytest.mark.parametrize("method", ["GET", "PUT", "PATCH", "DELETE"])
def test_control_rbac_name_is_blocked_before_upstream(control_proxy, resource, method):
    status, _, _ = request(
        control_proxy, f"/apis/rbac.authorization.k8s.io/v1/{resource}/mcp-server", method=method, body=b"{}"
    )
    assert status == 404
    assert FakeHTTPSConnection.requests == []


@pytest.mark.parametrize(
    "resource,kind",
    [
        ("roles", "Role"),
        ("rolebindings", "RoleBinding"),
        ("clusterroles", "ClusterRole"),
        ("clusterrolebindings", "ClusterRoleBinding"),
    ],
)
@pytest.mark.parametrize("method", ["POST", "PUT"])
def test_binary_application_rbac_is_inspected_and_forwarded(control_proxy, monkeypatch, resource, kind, method):
    decoded = {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": kind,
        "metadata": {"name": "application", "uid": "temporary-uid"},
    }
    if resource in {"roles", "rolebindings"}:
        decoded["metadata"]["namespace"] = "demo"
    if kind.endswith("Binding"):
        decoded.update(
            {
                "roleRef": {
                    "kind": "ClusterRole" if kind == "ClusterRoleBinding" else "Role",
                    "name": "application-role" if kind == "ClusterRoleBinding" else "mcp-server",
                },
                "subjects": [{"kind": "ServiceAccount", "name": "frontend", "namespace": "demo"}],
            }
        )
    else:
        decoded["rules"] = []
    calls = []

    def record_request(self, request_method, path, body=None, headers=None):
        self.path = path
        calls.append((request_method, path, body, headers))

    monkeypatch.setattr(FakeHTTPSConnection, "request", record_request)
    FakeHTTPSConnection.response = FakeResponse(json.dumps(decoded).encode())
    prefix = "/apis/rbac.authorization.k8s.io/v1"
    if resource in {"roles", "rolebindings"}:
        prefix += "/namespaces/demo"
    path = f"{prefix}/{resource}" + ("/application" if method == "PUT" else "")
    status, _, _ = request(
        control_proxy,
        path,
        method=method,
        body=b"k8s\x00rbac-fixture",
        headers={"Content-Type": "application/vnd.kubernetes.protobuf"},
    )
    assert status == 200
    assert calls[0][1].endswith("?dryRun=All")
    assert calls[-1][0:2] == (method, path)
    assert calls[-1][3]["Content-Type"] == "application/json"
    forwarded = json.loads(calls[-1][2])
    assert forwarded["kind"] == kind
    assert ("uid" in forwarded["metadata"]) is (method == "PUT")


@pytest.mark.parametrize(
    "resource,kind", [("rolebindings", "RoleBinding"), ("clusterrolebindings", "ClusterRoleBinding")]
)
def test_binary_private_rbac_never_persists(control_proxy, resource, kind):
    decoded = {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": kind,
        "metadata": {"name": "private-binding", "namespace": "demo" if kind == "RoleBinding" else ""},
        "roleRef": {"kind": "ClusterRole", "name": "view"},
        "subjects": [{"kind": "ServiceAccount", "name": "control", "namespace": MCP_CONTROL_NAMESPACE}],
    }
    FakeHTTPSConnection.response = FakeResponse(json.dumps(decoded).encode())
    prefix = "/apis/rbac.authorization.k8s.io/v1" + ("/namespaces/demo" if kind == "RoleBinding" else "")
    status, _, _ = request(
        control_proxy,
        f"{prefix}/{resource}",
        method="POST",
        body=b"k8s\x00rbac-fixture",
        headers={"Content-Type": "application/vnd.kubernetes.protobuf"},
    )
    assert status == 403
    assert len(FakeHTTPSConnection.requests) == 1
    assert FakeHTTPSConnection.requests[0][1].endswith("?dryRun=All")


@pytest.mark.parametrize("method", ["PUT", "PATCH", "DELETE"])
def test_private_binding_cannot_be_relabelled_or_deleted(control_proxy, method):
    binding = {
        "kind": "ClusterRoleBinding",
        "metadata": {"name": "private-binding"},
        "roleRef": {"kind": "ClusterRole", "name": "view"},
        "subjects": [{"kind": "ServiceAccount", "name": "control", "namespace": MCP_CONTROL_NAMESPACE}],
    }
    FakeHTTPSConnection.response = FakeResponse(json.dumps(binding).encode())
    status, _, _ = request(
        control_proxy,
        "/apis/rbac.authorization.k8s.io/v1/clusterrolebindings/private-binding",
        method=method,
        body=b'{"subjects":[]}',
        headers={"Content-Type": "application/json"},
    )
    assert status == 403
    assert [call[0] for call in FakeHTTPSConnection.requests] == ["GET"]


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_application_rbac_mutations_remain_usable(control_proxy, method):
    binding = {
        "kind": "RoleBinding",
        "metadata": {"name": "application-view", "namespace": "demo"},
        "roleRef": {"kind": "Role", "name": "mcp-server"},
        "subjects": [{"kind": "ServiceAccount", "name": "frontend", "namespace": "demo"}],
    }
    FakeHTTPSConnection.response = FakeResponse(json.dumps(binding).encode())
    path = "/apis/rbac.authorization.k8s.io/v1/namespaces/demo/rolebindings"
    if method != "POST":
        path += "/application-view"
    status, _, _ = request(
        control_proxy,
        path,
        method=method,
        body=json.dumps(binding).encode(),
        headers={"Content-Type": "application/json" if method != "PATCH" else "application/merge-patch+json"},
    )
    assert status == 200
    assert FakeHTTPSConnection.requests[-1][0] == method


@pytest.mark.parametrize("private", [False, True])
def test_rbac_bulk_delete_inspects_selected_objects(control_proxy, private):
    subject_namespace = MCP_CONTROL_NAMESPACE if private else "demo"
    binding = {
        "kind": "RoleBinding",
        "metadata": {"name": "selected", "namespace": "demo"},
        "roleRef": {"kind": "Role", "name": "application-role"},
        "subjects": [{"kind": "ServiceAccount", "name": "frontend", "namespace": subject_namespace}],
    }
    FakeHTTPSConnection.response = FakeResponse(json.dumps({"items": [binding]}).encode())
    path = "/apis/rbac.authorization.k8s.io/v1/namespaces/demo/rolebindings?labelSelector=app%3Dfrontend"
    status, _, _ = request(control_proxy, path, method="DELETE")
    assert status == (403 if private else 200)
    assert FakeHTTPSConnection.requests[0][1] == path
    assert [call[0] for call in FakeHTTPSConnection.requests] == (["GET"] if private else ["GET", "DELETE"])


@pytest.mark.parametrize("status", [403, 500])
def test_control_rbac_lookup_failure_blocks_mutation(control_proxy, status):
    FakeHTTPSConnection.response = FakeResponse(b'{"kind":"Status"}', status=status)
    result, _, _ = request(
        control_proxy, "/apis/rbac.authorization.k8s.io/v1/clusterrolebindings/application", method="PATCH", body=b"{}"
    )
    assert result == 502
    assert [call[0] for call in FakeHTTPSConnection.requests] == ["GET"]


@pytest.mark.parametrize("private", [False, True])
def test_upstream_status_and_warning_conceal_private_identity_only(control_proxy, private):
    message = (
        "User system:serviceaccount:sregym:mcp-server cannot list serviceaccounts"
        if private
        else "Invalid field spec.image"
    )
    payload = json.dumps({"kind": "Status", "message": message, "code": 403}).encode()
    FakeHTTPSConnection.response = FakeResponse(
        payload, status=403, headers=[("Content-Type", "application/json"), ("Warning", message)]
    )
    status, headers, body = request(control_proxy, "/api/v1/serviceaccounts")
    assert status == 403
    if private:
        assert b"sregym" not in body and b"mcp-server" not in body
        assert "Warning" not in headers
        assert json.loads(body)["code"] == 403
    else:
        assert body == payload
        assert headers["Warning"] == message


def test_generated_agent_kubeconfig_uses_neutral_names_and_preserves_authentication(proxy, tmp_path):
    path = proxy.generate_agent_kubeconfig(str(tmp_path / "config"))
    text = Path(path).read_text()
    data = yaml.safe_load(text)
    assert "sregym" not in text.lower()
    assert data["current-context"] == data["contexts"][0]["name"]
    context = data["contexts"][0]["context"]
    assert context["cluster"] == data["clusters"][0]["name"]
    assert context["user"] == data["users"][0]["name"]
    assert data["users"][0]["user"]["token"] == proxy._agent_token
    assert data["clusters"][0]["cluster"]["server"] == proxy.get_proxy_url()
    assert data["clusters"][0]["cluster"]["certificate-authority-data"]


def test_generated_agent_kubeconfig_uses_real_verified_tls_and_token(proxy):
    # The local proxy listener and its generated CA/token are real; upstream
    # response transport remains the existing fixture, not a cluster claim.
    proxy.listen_port = proxy.server.server_address[1]
    path = Path(proxy.generate_agent_kubeconfig())
    assert path.name.startswith("operator-kubeconfig-")
    assert path.stat().st_mode & 0o777 == 0o600
    data = yaml.safe_load(path.read_text())
    cluster = data["clusters"][0]["cluster"]
    endpoint = urlsplit(cluster["server"])
    context = ssl.create_default_context(cadata=base64.b64decode(cluster["certificate-authority-data"]).decode())
    token = data["users"][0]["user"]["token"]
    FakeHTTPSConnection.response = FakeResponse(b'{"kind":"PodList","items":[]}')

    def send(token):
        with (
            socket.create_connection((endpoint.hostname, endpoint.port), timeout=5) as raw_socket,
            context.wrap_socket(raw_socket, server_hostname=endpoint.hostname) as connection,
        ):
            connection.sendall(
                (
                    f"GET /api/v1/namespaces/app/pods HTTP/1.1\r\nHost: {endpoint.hostname}\r\n"
                    f"Authorization: Bearer {token}\r\nConnection: close\r\n\r\n"
                ).encode()
            )
            response = http.client.HTTPResponse(connection)
            response.begin()
            return response.status, response.read()

    status, payload = send(token)
    assert status == 200 and json.loads(payload) == {"kind": "PodList", "items": []}
    assert len(FakeHTTPSConnection.requests) == 1
    assert send("wrong-token")[0] == 401
    assert len(FakeHTTPSConnection.requests) == 1
    with (
        socket.create_connection((endpoint.hostname, endpoint.port), timeout=5) as raw_socket,
        pytest.raises(ssl.SSLCertVerificationError),
    ):
        ssl.create_default_context().wrap_socket(raw_socket, server_hostname=endpoint.hostname)
    proxy.stop()
    assert not path.exists()


def test_upstream_merge_skips_both_generated_config_prefixes(monkeypatch, tmp_path):
    paths = [
        tmp_path / name
        for name in (
            "sregym-agent-kubeconfig-old.yaml",
            "first-cluster.yaml",
            "operator-kubeconfig-new.yaml",
            "second-cluster.yaml",
        )
    ]
    monkeypatch.setenv("KUBECONFIG", os.path.pathsep.join(map(str, paths)))
    assert KubernetesAPIProxy._resolve_upstream_kubeconfig() == os.path.pathsep.join(map(str, (paths[1], paths[3])))
    monkeypatch.setenv("KUBECONFIG", os.path.pathsep.join(map(str, (paths[0], paths[2]))))
    assert KubernetesAPIProxy._resolve_upstream_kubeconfig() == os.path.expanduser("~/.kube/config")
