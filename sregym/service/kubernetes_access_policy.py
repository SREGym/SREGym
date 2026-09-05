"""Shared Kubernetes restrictions, independent of proxy HTTP handling."""

from copy import deepcopy

PROTECTED_EGRESS_RESOURCES = {
    "felixconfigurations",
    "globalnetworkpolicies",
    "networkpolicies",
    "tiers",
}
PROTECTED_EGRESS_CRDS = {
    "adminnetworkpolicies.policy.networking.k8s.io",
    "baselineadminnetworkpolicies.policy.networking.k8s.io",
} | {f"{resource}.crd.projectcalico.org" for resource in PROTECTED_EGRESS_RESOURCES}


def restricted_cluster_role(role: dict) -> dict:
    """Return a copy with outbound-policy controls limited to read access."""
    result = deepcopy(role)
    for rule in result["rules"]:
        if "crd.projectcalico.org" in rule["apiGroups"]:
            rule["resources"] = [r for r in rule["resources"] if r not in PROTECTED_EGRESS_RESOURCES]
    result["rules"] = [rule for rule in result["rules"] if rule["resources"]]
    result["rules"].append(
        {
            "apiGroups": ["crd.projectcalico.org"],
            "resources": sorted(PROTECTED_EGRESS_RESOURCES),
            "verbs": ["get", "list", "watch"],
        }
    )
    return result


def contains_network_escape(value) -> bool:
    """Return whether a workload mutation can escape pod network isolation."""
    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                path = str(item.get("path", "")).rstrip("/").casefold()
                # copy/move take their value from the stored object, not the
                # patch body. A metadata-only target cannot change a PodSpec;
                # other targets require object resolution and are not accepted.
                if item.get("op") in {"copy", "move"} and not path.startswith("/metadata/"):
                    return True
                if path.endswith(("/hostnetwork", "/hostpid", "/hostipc", "/privileged")) and item.get("value") is True:
                    return True
                if path.endswith("/hostpath") and item.get("op", "").casefold() != "remove":
                    return True
            if contains_network_escape(item):
                return True
        return False
    if not isinstance(value, dict):
        return False

    for key, item in value.items():
        normalized_key = str(key).casefold()
        if normalized_key in {"hostnetwork", "hostpid", "hostipc", "privileged"} and item is True:
            return True
        if normalized_key == "hostpath" and item is not None:
            return True
        if contains_network_escape(item):
            return True
    return False
