"""Agent-visible names and metadata for benchmark infrastructure."""

import json

HIDDEN_NAMESPACES: set[str] = {"chaos-mesh", "khaos"}
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


def is_chaos_event(resource: dict, hidden_namespaces: set[str]) -> bool:
    # Kubernetes omits kind from items in an EventList, including watch events.
    if resource.get("kind") != "Event" and "involvedObject" not in resource and "regarding" not in resource:
        return False
    references = (resource.get("involvedObject") or {}, resource.get("regarding") or {})
    return any(ref.get("namespace") in hidden_namespaces for ref in references) or mentions_chaos_mesh(
        json.dumps(resource)
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
