"""Shared rules for the Kubernetes and observability data shown to agents."""

import json

MCP_CONTROL_NAMESPACE = "sregym"
VERIFIER_PROBE_NAMESPACE = "sregym-verifier"
# Keep existing control-service identities while excluding them from agent IO.
HIDDEN_NAMESPACES: set[str] = {"chaos-mesh", "khaos", MCP_CONTROL_NAMESPACE, VERIFIER_PROBE_NAMESPACE}
HIDDEN_LABELS: dict[str, set[str]] = {
    "app": {"load-generator", "locust-fetcher"},
    "job": {"workload"},
    "network-access": {"restricted"},
    "opentelemetry.io/name": {"load-generator"},
    "app.kubernetes.io/component": {"mcp-server"},
}
HELM_RELEASE_SECRET_TYPE = "helm.sh/release.v1"
HELM_RELEASE_SECRET_NAME_PREFIX = "sh.helm.release.v1."
CHAOS_API_GROUP = "chaos-mesh.org"
CHAOS_MARKERS = ("chaos-mesh", "chaos-controller-manager", "chaos-daemon")
CLUSTER_CONTROL_PLANE_RESOURCES = {
    "apiservices",
    "clusterrolebindings",
    "clusterroles",
    "customresourcedefinitions",
    "mutatingwebhookconfigurations",
    "validatingwebhookconfigurations",
}


def mentions_chaos_mesh(value: str) -> bool:
    return any(marker in value.casefold() for marker in CHAOS_MARKERS)


def is_hidden_api_group(group: str) -> bool:
    return group == CHAOS_API_GROUP


def is_hidden_cluster_resource(resource: str | None, name: str | None) -> bool:
    return resource in CLUSTER_CONTROL_PLANE_RESOURCES and bool(name and mentions_chaos_mesh(name))


def is_helm_release_secret(resource: dict) -> bool:
    """Identify Helm's stored release record, which contains rendered manifests."""
    metadata = resource.get("metadata") or {}
    name = metadata.get("name") or ""
    return resource.get("type") == HELM_RELEASE_SECRET_TYPE or name.startswith(HELM_RELEASE_SECRET_NAME_PREFIX)


def is_chaos_event(resource: dict, hidden_namespaces: set[str]) -> bool:
    # Kubernetes omits kind from items in an EventList, including watch events.
    if resource.get("kind") != "Event" and "involvedObject" not in resource and "regarding" not in resource:
        return False
    references = (resource.get("involvedObject") or {}, resource.get("regarding") or {})
    return any(ref.get("namespace") in hidden_namespaces for ref in references) or mentions_chaos_mesh(
        json.dumps(resource)
    )


def is_hidden_resource(resource: dict, hidden_namespaces: set[str], hidden_labels: dict[str, set[str]]) -> bool:
    """Return whether a Kubernetes object must not be visible to an agent."""
    metadata = resource.get("metadata") or {}
    labels = metadata.get("labels") or {}
    has_hidden_label = any(labels.get(key) in values for key, values in hidden_labels.items())
    has_hidden_subject = bool(resource.get("roleRef")) and any(
        isinstance(subject, dict) and subject.get("namespace") in hidden_namespaces
        for subject in resource.get("subjects") or []
    )
    return (
        metadata.get("namespace") in hidden_namespaces
        or has_hidden_label
        or has_hidden_subject
        or is_helm_release_secret(resource)
        or is_chaos_event(resource, hidden_namespaces)
        or (not metadata.get("namespace") and mentions_chaos_mesh(str(metadata.get("name", ""))))
    )


def sanitize_visible_resource(resource: dict) -> dict:
    """Remove Chaos controller bookkeeping, but preserve real workload state."""
    metadata = resource.get("metadata")
    if not isinstance(metadata, dict):
        return resource
    for field in ("annotations", "labels"):
        values = metadata.get(field)
        if isinstance(values, dict):
            metadata[field] = {
                key: value
                for key, value in values.items()
                if not mentions_chaos_mesh(key) and not (isinstance(value, str) and mentions_chaos_mesh(value))
            }
    fields = metadata.get("managedFields")
    if isinstance(fields, list):
        metadata["managedFields"] = [field for field in fields if not mentions_chaos_mesh(json.dumps(field))]
    return resource


def filter_namespace_list(data: dict, hidden_namespaces: set[str]) -> dict:
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


def filter_resource_list(data: dict, hidden_namespaces: set[str], hidden_labels: dict[str, set[str]]) -> dict:
    """Remove hidden objects from standard and Table list responses."""
    if "items" in data:
        data["items"] = [
            sanitize_visible_resource(item)
            for item in data["items"]
            if not is_hidden_resource(item, hidden_namespaces, hidden_labels)
        ]
    if "rows" in data:
        data["rows"] = [
            row
            for row in data["rows"]
            if isinstance(row.get("object"), dict)
            and not is_hidden_resource(row["object"], hidden_namespaces, hidden_labels)
        ]
        for row in data["rows"]:
            sanitize_visible_resource(row["object"])
    return data


def filter_api_groups(data: dict) -> dict:
    """Hide the Chaos Mesh API group from Kubernetes discovery."""
    if isinstance(data.get("groups"), list):
        data["groups"] = [group for group in data["groups"] if not is_hidden_api_group(group.get("name"))]
    return data


def filter_openapi_document(data: dict, version: str) -> dict:
    """Remove Chaos API schemas without changing unrelated Kubernetes schemas."""
    if version == "openapi_v2":
        for field in ("definitions", "paths"):
            if isinstance(data.get(field), dict):
                data[field] = {key: value for key, value in data[field].items() if not mentions_chaos_mesh(key)}
    elif version == "openapi_v3" and isinstance(data.get("paths"), dict):
        data["paths"] = {key: value for key, value in data["paths"].items() if not mentions_chaos_mesh(key)}
    return data


def visible_log_value(value: str) -> bool:
    return value not in HIDDEN_NAMESPACES and not mentions_chaos_mesh(value)


def visible_observability_record(value: object) -> bool:
    """Hide a metric or alert if any of its labels or text reveal noise infrastructure."""
    if isinstance(value, str):
        return visible_log_value(value)
    if isinstance(value, dict):
        return all(
            visible_observability_record(key) and visible_observability_record(item) for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return all(visible_observability_record(item) for item in value)
    return True
