"""
Kubernetes API Filtering Proxy

This proxy sits between agents and the Kubernetes API server, filtering out
chaos engineering namespaces (chaos-mesh, khaos) and load generator resources
from API responses to prevent agents from discovering that faults are being
injected via chaos tools or that traffic is synthetic.

The proxy:
1. Forwards all requests to the real Kubernetes API
2. Filters namespace listings to exclude hidden namespaces
3. Returns 403 Forbidden for direct access to hidden namespaces or hidden resources
4. Filters cluster-wide resource listings to exclude resources in hidden namespaces
5. Filters resources with hidden labels (e.g. load generators) from list responses
"""

import base64
import contextlib
import json
import logging
import os
import ssl
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse, urlsplit

import urllib3
import yaml
from kubernetes import config

logger = logging.getLogger("all.infra.k8s_proxy")
logger.propagate = True
logger.setLevel(logging.DEBUG)

# Namespaces to hide from agents
HIDDEN_NAMESPACES: set[str] = {"chaos-mesh", "khaos"}

# Labels to hide from agents - resources matching any of these label key/value pairs are hidden.
# Load generators produce synthetic traffic and should not be visible to agents.
HIDDEN_LABELS: dict[str, set[str]] = {
    "app": {"load-generator", "locust-fetcher"},
    "job": {"workload"},
    "opentelemetry.io/name": {"load-generator"},
}

# Helm stores each release revision in a Secret by default. These Secrets contain
# the complete rendered chart, including the application's pre-fault manifests.
HELM_RELEASE_SECRET_TYPE = "helm.sh/release.v1"
HELM_RELEASE_SECRET_NAME_PREFIX = "sh.helm.release.v1."
WORKLOAD_RESOURCES = {
    "cronjobs",
    "daemonsets",
    "deployments",
    "jobs",
    "pods",
    "replicasets",
    "replicationcontrollers",
    "statefulsets",
}
STRUCTURED_KUBERNETES_CONTENT_TYPES = {
    "application/apply-patch+yaml",
    "application/json",
    "application/json-patch+json",
    "application/merge-patch+json",
    "application/strategic-merge-patch+json",
    "application/yaml",
}


def _decode_path_parts(path: str) -> list[str]:
    """Return normalized path segments after decoding nested URL encoding."""
    decoded_path = urlsplit(path).path
    for _ in range(3):
        next_path = unquote(decoded_path)
        if next_path == decoded_path:
            break
        decoded_path = next_path

    parts: list[str] = []
    for part in decoded_path.split("/"):
        if not part or part == ".":
            continue
        if part == "..":
            if parts:
                parts.pop()
            continue
        parts.append(part)
    return parts


def _resource_request(path: str) -> tuple[str | None, str | None]:
    """Return the Kubernetes resource and optional object name in an API path."""
    parts = _decode_path_parts(path)
    if len(parts) >= 2 and parts[0] == "api":
        resource_index = 2
    elif len(parts) >= 3 and parts[0] == "apis":
        resource_index = 3
    else:
        return None, None

    if len(parts) <= resource_index:
        return None, None

    # A namespaced resource path contains /namespaces/{namespace}/{resource}.
    # /api/v1/namespaces and /api/v1/namespaces/{name} are Namespace paths.
    if parts[resource_index] == "namespaces" and len(parts) >= resource_index + 3:
        resource_index += 2

    resource = parts[resource_index]
    name = parts[resource_index + 1] if len(parts) > resource_index + 1 else None
    return resource, name


def _is_watch_request(path: str) -> bool:
    """Return whether a Kubernetes request asks for a watch stream."""
    query = urlsplit(path).query
    for _ in range(3):
        next_query = unquote(query)
        if next_query == query:
            break
        query = next_query
    values = parse_qs(query, keep_blank_values=True).get("watch", [])
    return any(value.lower() in {"1", "true"} for value in values)


def _is_helm_release_secret(resource: dict) -> bool:
    """Return whether a Kubernetes object is Helm's stored release record."""
    metadata = resource.get("metadata") or {}
    name = metadata.get("name") or ""
    return resource.get("type") == HELM_RELEASE_SECRET_TYPE or name.startswith(HELM_RELEASE_SECRET_NAME_PREFIX)


def _has_hidden_label(metadata: dict, hidden_labels: dict[str, set[str]]) -> bool:
    """Return whether resource metadata contains a configured hidden label."""
    labels = metadata.get("labels") or {}
    return any(labels.get(key) in values for key, values in hidden_labels.items())


def _is_hidden_resource(
    resource: dict,
    hidden_namespaces: set[str],
    hidden_labels: dict[str, set[str]],
) -> bool:
    """Return whether a Kubernetes object must not be visible to an agent."""
    metadata = resource.get("metadata") or {}
    return (
        metadata.get("namespace") in hidden_namespaces
        or _has_hidden_label(metadata, hidden_labels)
        or _is_helm_release_secret(resource)
    )


def _filter_namespace_list(data: dict, hidden_namespaces: set[str]) -> dict:
    """Remove hidden namespaces from standard and Table list responses."""
    if "items" in data:
        data["items"] = [
            item for item in data["items"] if item.get("metadata", {}).get("name") not in hidden_namespaces
        ]
    if "rows" in data:
        data["rows"] = [
            row
            for row in data["rows"]
            if isinstance(row.get("object"), dict)
            and row["object"].get("metadata", {}).get("name") not in hidden_namespaces
        ]
    return data


def _filter_resource_list(
    data: dict,
    hidden_namespaces: set[str],
    hidden_labels: dict[str, set[str]],
) -> dict:
    """Remove hidden objects from standard and Table list responses."""
    if "items" in data:
        data["items"] = [
            item for item in data["items"] if not _is_hidden_resource(item, hidden_namespaces, hidden_labels)
        ]
    if "rows" in data:
        data["rows"] = [
            row
            for row in data["rows"]
            if isinstance(row.get("object"), dict)
            and not _is_hidden_resource(row["object"], hidden_namespaces, hidden_labels)
        ]
    return data


def _is_hidden_namespace_request(path: str, hidden_namespaces: set[str]) -> bool:
    """Return whether a decoded API path addresses a hidden namespace."""
    parts = _decode_path_parts(path)
    for index, part in enumerate(parts):
        if part == "namespaces" and index + 1 < len(parts) and parts[index + 1] in hidden_namespaces:
            return True
    return False


def _response_filter_type(path: str) -> str | None:
    """Return the list-response filter required for a Kubernetes API path."""
    resource, name = _resource_request(path)
    if resource == "namespaces" and name is None:
        return "namespaces"
    if resource is not None and name is None:
        return "resources"
    return None


def _is_helm_release_secret_request(path: str) -> bool:
    """Return whether a path directly addresses a Helm release Secret."""
    resource, name = _resource_request(path)
    return resource == "secrets" and bool(name and name.startswith(HELM_RELEASE_SECRET_NAME_PREFIX))


def _is_secret_collection_delete(path: str, method: str) -> bool:
    """Return whether a request can bulk-delete Helm release Secrets."""
    resource, name = _resource_request(path)
    return method == "DELETE" and resource == "secrets" and name is None


def _is_secret_watch_request(path: str) -> bool:
    """Return whether a request can stream Helm release Secret events."""
    resource, _ = _resource_request(path)
    return resource == "secrets" and _is_watch_request(path)


def _requires_json_secret_response(path: str, method: str) -> bool:
    """Return whether the proxy must inspect a Secret response as JSON."""
    resource, _ = _resource_request(path)
    return method == "GET" and resource == "secrets"


def _contains_helm_release_secret_name(value) -> bool:
    """Return whether structured request data refers to a Helm release Secret."""
    if isinstance(value, str):
        return value.startswith(HELM_RELEASE_SECRET_NAME_PREFIX)
    if isinstance(value, list):
        return any(_contains_helm_release_secret_name(item) for item in value)
    if isinstance(value, dict):
        return any(_contains_helm_release_secret_name(item) for item in value.values())
    return False


def _inspect_workload_request(path: str, method: str, body: bytes | None, content_type: str) -> str | None:
    """Inspect workload mutations for references to a protected Helm Secret.

    Returns ``forbidden`` for a Helm Secret reference, ``unsupported`` when a
    workload body cannot be inspected safely, and ``None`` when it is safe.
    """
    resource, _ = _resource_request(path)
    if method not in {"POST", "PUT", "PATCH"} or resource not in WORKLOAD_RESOURCES or not body:
        return None

    media_type = content_type.partition(";")[0].strip().lower()
    if media_type not in STRUCTURED_KUBERNETES_CONTENT_TYPES:
        return "unsupported"

    try:
        if media_type.endswith("+yaml") or media_type == "application/yaml":
            data = yaml.safe_load(body)
        else:
            data = json.loads(body)
    except (UnicodeDecodeError, ValueError, yaml.YAMLError):
        return "unsupported"

    if _contains_helm_release_secret_name(data):
        return "forbidden"
    return None


# Disable SSL warnings for self-signed certs
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class KubernetesAPIProxy:
    """Manages the Kubernetes API filtering proxy."""

    # Paths used when running inside a Kubernetes pod
    _INCLUSTER_TOKEN_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/token"
    _INCLUSTER_CA_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"

    def __init__(
        self,
        hidden_namespaces: set[str] | None = None,
        hidden_labels: dict[str, set[str]] | None = None,
        listen_port: int = 6443,
    ):
        self.hidden_namespaces: set[str] = hidden_namespaces if hidden_namespaces is not None else HIDDEN_NAMESPACES
        self.hidden_labels: dict[str, set[str]] = hidden_labels if hidden_labels is not None else HIDDEN_LABELS
        self.listen_port = listen_port
        self.server: ThreadingHTTPServer | None = None
        self.server_thread: threading.Thread | None = None
        self._temp_files: list = []
        self._bearer_token: str | None = None

        if os.path.exists(self._INCLUSTER_TOKEN_PATH):
            # Running inside a Kubernetes pod — use ServiceAccount credentials
            logger.info("Detected in-cluster environment; using ServiceAccount token for upstream auth")
            with open(self._INCLUSTER_TOKEN_PATH) as f:
                self._bearer_token = f.read().strip()
            with open(self._INCLUSTER_CA_PATH) as f:
                self.ca_cert = f.read()
            self.client_cert = None
            self.client_key = None
            self.api_host = os.environ.get("KUBERNETES_SERVICE_HOST", "kubernetes.default.svc")
            self.api_port = int(os.environ.get("KUBERNETES_SERVICE_PORT", "443"))
        else:
            # Running outside the cluster — load from kubeconfig
            # Always load from the default kubeconfig path, ignoring KUBECONFIG env var
            # This prevents circular dependency if KUBECONFIG points to our proxy
            default_kubeconfig = os.path.expanduser("~/.kube/config")
            config.load_kube_config(config_file=default_kubeconfig)
            self.api_host, self.api_port, self.ca_cert, self.client_cert, self.client_key = self._load_cluster_config(
                kubeconfig_path=default_kubeconfig
            )

    def _load_cluster_config(self, kubeconfig_path: str | None = None):
        """Extract API server connection details from kubeconfig."""
        # Load full kubeconfig
        if kubeconfig_path is None:
            kubeconfig_path = os.path.expanduser("~/.kube/config")

        # Get the current context's cluster and user from the explicit config file
        _, active_context = config.list_kube_config_contexts(config_file=kubeconfig_path)
        cluster_name = active_context["context"]["cluster"]
        user_name = active_context["context"]["user"]
        with open(kubeconfig_path) as f:
            kubeconfig = yaml.safe_load(f)

        # Find cluster config
        cluster_config = None
        for cluster in kubeconfig["clusters"]:
            if cluster["name"] == cluster_name:
                cluster_config = cluster["cluster"]
                break

        # Find user config
        user_config = None
        for user in kubeconfig["users"]:
            if user["name"] == user_name:
                user_config = user["user"]
                break

        if not cluster_config:
            raise ValueError(f"Cluster {cluster_name} not found in kubeconfig")

        # Parse API server URL
        server_url = cluster_config["server"]
        parsed = urlparse(server_url)
        api_host = parsed.hostname
        api_port = parsed.port or 443

        # Get CA cert (might be inline or file path)
        ca_cert = None
        if "certificate-authority-data" in cluster_config:
            ca_cert = base64.b64decode(cluster_config["certificate-authority-data"]).decode()
        elif "certificate-authority" in cluster_config:
            with open(cluster_config["certificate-authority"]) as f:
                ca_cert = f.read()

        # Get client cert and key
        client_cert = None
        client_key = None
        if user_config:
            if "client-certificate-data" in user_config:
                client_cert = base64.b64decode(user_config["client-certificate-data"]).decode()
            elif "client-certificate" in user_config:
                with open(user_config["client-certificate"]) as f:
                    client_cert = f.read()

            if "client-key-data" in user_config:
                client_key = base64.b64decode(user_config["client-key-data"]).decode()
            elif "client-key" in user_config:
                with open(user_config["client-key"]) as f:
                    client_key = f.read()

        return api_host, api_port, ca_cert, client_cert, client_key

    def _create_temp_cert_files(self):
        """Create temporary files for certificates."""
        files = {}

        if self.ca_cert:
            with tempfile.NamedTemporaryFile(mode="w", suffix=".crt", delete=False) as ca_file:
                ca_file.write(self.ca_cert)
            files["ca"] = ca_file.name
            self._temp_files.append(ca_file.name)

        if self.client_cert:
            with tempfile.NamedTemporaryFile(mode="w", suffix=".crt", delete=False) as cert_file:
                cert_file.write(self.client_cert)
            files["cert"] = cert_file.name
            self._temp_files.append(cert_file.name)

        if self.client_key:
            with tempfile.NamedTemporaryFile(mode="w", suffix=".key", delete=False) as key_file:
                key_file.write(self.client_key)
            files["key"] = key_file.name
            self._temp_files.append(key_file.name)

        return files

    def start(self):
        """Start the proxy server in a background thread."""
        cert_files = self._create_temp_cert_files()
        hidden_namespaces = self.hidden_namespaces
        hidden_labels = self.hidden_labels
        api_host = self.api_host
        api_port = self.api_port
        bearer_token = self._bearer_token

        class FilteringProxyHandler(BaseHTTPRequestHandler):
            """HTTP request handler that proxies and filters Kubernetes API responses."""

            def log_message(self, format, *args):
                logger.debug(f"Proxy: {format % args}")

            def _get_upstream_connection(self):
                """Create HTTPS connection to upstream Kubernetes API."""
                import http.client

                context = ssl.create_default_context()
                if cert_files.get("ca"):
                    context.load_verify_locations(cert_files["ca"])
                else:
                    context.check_hostname = False
                    context.verify_mode = ssl.CERT_NONE

                if cert_files.get("cert") and cert_files.get("key"):
                    context.load_cert_chain(cert_files["cert"], cert_files["key"])

                return http.client.HTTPSConnection(api_host, api_port, context=context)

            def _proxy_request(self, method: str):
                """Proxy request to upstream API and filter response."""
                path = self.path

                # Block direct access to hidden namespaces
                if _is_hidden_namespace_request(path, hidden_namespaces):
                    self.send_error(403, "Forbidden: Access to this namespace is not allowed")
                    return

                # A Helm release Secret is benchmark source data, not runtime
                # evidence. Block direct access before forwarding so an agent
                # cannot read, relabel, replace, or delete the stored manifest.
                if _is_helm_release_secret_request(path):
                    self.send_error(403, "Forbidden: Access to this resource is not allowed")
                    return

                # A Secret watch would stream the filtered records as events.
                if _is_secret_watch_request(path):
                    self.send_error(403, "Forbidden: Watching Secrets is not allowed")
                    return

                # DELETE on a Secret collection can target Helm records through
                # a label or field selector without putting their names in the URL.
                if _is_secret_collection_delete(path, method):
                    self.send_error(403, "Forbidden: Bulk deletion of Secrets is not allowed")
                    return

                # Read request body if present
                content_length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(content_length) if content_length > 0 else None

                workload_inspection = _inspect_workload_request(
                    path,
                    method,
                    body,
                    self.headers.get("Content-Type", ""),
                )
                if workload_inspection == "forbidden":
                    self.send_error(403, "Forbidden: Workloads cannot reference this Secret")
                    return
                if workload_inspection == "unsupported":
                    self.send_error(415, "Unsupported Media Type: Workload request body cannot be inspected")
                    return

                # Forward request to upstream
                try:
                    conn = self._get_upstream_connection()
                    filter_type = _response_filter_type(path)
                    requires_json = _requires_json_secret_response(path, method) or (
                        method == "GET" and filter_type is not None and not _is_watch_request(path)
                    )
                    # Forward headers (except Host and Accept-Encoding to avoid gzip)
                    excluded_headers = {"host", "accept-encoding"}
                    if requires_json:
                        excluded_headers.add("accept")
                    headers = {k: v for k, v in self.headers.items() if k.lower() not in excluded_headers}
                    if requires_json:
                        # The proxy cannot safely filter Kubernetes' protobuf form.
                        headers["Accept"] = "application/json"
                    # In-cluster mode: authenticate to the API server with the ServiceAccount bearer token
                    if bearer_token:
                        headers["Authorization"] = f"Bearer {bearer_token}"
                    conn.request(method, path, body=body, headers=headers)
                    response = conn.getresponse()

                    # Read response
                    response_body = response.read()
                    content_type = response.getheader("Content-Type", "")
                    content_encoding = response.getheader("Content-Encoding", "")

                    # Decompress if gzip-encoded
                    if content_encoding == "gzip":
                        import gzip

                        response_body = gzip.decompress(response_body)

                    # Filter successful JSON responses. Secret reads and resource
                    # lists fail closed if the upstream does not return usable JSON.
                    if response.status == 200 and requires_json and "application/json" not in content_type:
                        self.send_error(502, "Bad Gateway: Kubernetes returned an unsupported response format")
                        conn.close()
                        return

                    if response.status == 200 and "application/json" in content_type:
                        try:
                            data = json.loads(response_body)
                            if filter_type == "namespaces":
                                data = _filter_namespace_list(data, hidden_namespaces)
                                response_body = json.dumps(data).encode()
                            elif filter_type == "resources":
                                data = _filter_resource_list(data, hidden_namespaces, hidden_labels)
                                response_body = json.dumps(data).encode()
                            elif filter_type is None and _is_hidden_resource(data, hidden_namespaces, hidden_labels):
                                # Block direct access to individual hidden resources
                                self.send_error(403, "Forbidden: Access to this resource is not allowed")
                                conn.close()
                                return
                        except json.JSONDecodeError:
                            if requires_json:
                                self.send_error(502, "Bad Gateway: Kubernetes returned invalid JSON")
                                conn.close()
                                return

                    # Send response to client
                    self.send_response(response.status)
                    for header, value in response.getheaders():
                        # Skip headers we're modifying
                        if header.lower() not in ("transfer-encoding", "content-length", "content-encoding"):
                            self.send_header(header, value)
                    self.send_header("Content-Length", str(len(response_body)))
                    self.end_headers()
                    self.wfile.write(response_body)

                    conn.close()

                except BrokenPipeError:
                    # Client closed while agent still has in-flight request open. Ignore
                    pass
                except Exception as e:
                    logger.error(f"Proxy error: {e}")
                    self.send_error(502, f"Bad Gateway: {str(e)}")

            def do_GET(self):
                self._proxy_request("GET")

            def do_POST(self):
                self._proxy_request("POST")

            def do_PUT(self):
                self._proxy_request("PUT")

            def do_PATCH(self):
                self._proxy_request("PATCH")

            def do_DELETE(self):
                self._proxy_request("DELETE")

            def do_OPTIONS(self):
                self._proxy_request("OPTIONS")

            def do_HEAD(self):
                self._proxy_request("HEAD")

        # Create and start server
        self.server = ThreadingHTTPServer(("127.0.0.1", self.listen_port), FilteringProxyHandler)
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()
        logger.info(f"Kubernetes API filtering proxy started on port {self.listen_port}")
        logger.info(f"Hidden namespaces: {self.hidden_namespaces}")
        logger.info(f"Hidden labels: {self.hidden_labels}")

    def stop(self):
        """Stop the proxy server."""
        if self.server:
            self.server.shutdown()
            self.server = None
            self.server_thread = None
            logger.info("Kubernetes API filtering proxy stopped")

        # Cleanup temp files
        for temp_file in self._temp_files:
            with contextlib.suppress(OSError):
                os.unlink(temp_file)
        self._temp_files = []

    def generate_agent_kubeconfig(self, output_path: str | None = None) -> str:
        """
        Generate a kubeconfig file for agents that points to this proxy.

        Args:
            output_path: Path to write kubeconfig. If None, writes to temp file.

        Returns:
            Path to the generated kubeconfig file.
        """
        import yaml

        kubeconfig = {
            "apiVersion": "v1",
            "kind": "Config",
            "current-context": "sregym-agent",
            "clusters": [
                {
                    "name": "sregym-proxy",
                    "cluster": {
                        # Use HTTP since proxy runs locally without TLS
                        "server": f"http://127.0.0.1:{self.listen_port}",
                        # Skip TLS verification for local proxy
                        "insecure-skip-tls-verify": True,
                    },
                }
            ],
            "contexts": [
                {
                    "name": "sregym-agent",
                    "context": {
                        "cluster": "sregym-proxy",
                        "user": "sregym-agent",
                    },
                }
            ],
            "users": [
                {
                    "name": "sregym-agent",
                    # No credentials needed - proxy handles auth to real API
                    "user": {},
                }
            ],
        }

        if output_path is None:
            output_path = os.path.join(tempfile.gettempdir(), "sregym-agent-kubeconfig")

        with open(output_path, "w") as f:
            yaml.dump(kubeconfig, f)

        logger.info(f"Generated agent kubeconfig at {output_path}")
        return output_path

    def get_proxy_url(self) -> str:
        """Get the URL of the proxy server."""
        return f"http://127.0.0.1:{self.listen_port}"


# Module-level singleton for easy access
_proxy_instance: KubernetesAPIProxy | None = None


def get_proxy() -> KubernetesAPIProxy:
    """Get or create the singleton proxy instance."""
    global _proxy_instance
    if _proxy_instance is None:
        _proxy_instance = KubernetesAPIProxy()
    return _proxy_instance


def start_proxy(
    hidden_namespaces: set[str] | None = None,
    hidden_labels: dict[str, set[str]] | None = None,
    port: int = 16443,
) -> KubernetesAPIProxy:
    """Start the Kubernetes API filtering proxy."""
    global _proxy_instance
    if _proxy_instance is not None:
        _proxy_instance.stop()
    _proxy_instance = KubernetesAPIProxy(
        hidden_namespaces=hidden_namespaces, hidden_labels=hidden_labels, listen_port=port
    )
    _proxy_instance.start()
    return _proxy_instance


def stop_proxy():
    """Stop the Kubernetes API filtering proxy."""
    global _proxy_instance
    if _proxy_instance is not None:
        _proxy_instance.stop()
        _proxy_instance = None
