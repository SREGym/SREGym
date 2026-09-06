"""Shared Kubernetes restrictions, independent of proxy HTTP handling."""

from copy import deepcopy

import jsonpatch

EGRESS_POLICY_TIER = "adminnetworkpolicy"
EGRESS_POLICY_NAME = f"{EGRESS_POLICY_TIER}.external-egress-boundary"
CALICO_POLICY_RESOURCES = {"globalnetworkpolicies", "networkpolicies"}

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
    """Protect global controls; the proxy validates individual policy writes."""
    result = deepcopy(role)
    read_only = PROTECTED_EGRESS_RESOURCES - CALICO_POLICY_RESOURCES
    for rule in result["rules"]:
        if "crd.projectcalico.org" in rule["apiGroups"]:
            rule["resources"] = [r for r in rule["resources"] if r not in read_only]
    result["rules"] = [rule for rule in result["rules"] if rule["resources"]]
    result["rules"].append(
        {
            "apiGroups": ["crd.projectcalico.org"],
            "resources": sorted(read_only),
            "verbs": ["get", "list", "watch"],
        }
    )
    return result


def workload_network_settings(resource: str, value: dict) -> dict:
    """Extract restricted PodSpec fields, ignoring similarly named user data.

    Container and volume names identify existing permissions. An update cannot
    transfer a privileged setting or host path to a newly named container or volume.
    """
    path = ("spec",)
    if resource == "cronjobs":
        path = ("spec", "jobTemplate", "spec", "template", "spec")
    elif resource != "pods":
        path = ("spec", "template", "spec")
    spec = value
    for key in path:
        if not isinstance(spec, dict):
            return {}
        spec = spec.get(key) or {}
    if not isinstance(spec, dict):
        return {}
    settings = {key: True for key in ("hostNetwork", "hostPID", "hostIPC") if spec.get(key) is True}
    for category in ("containers", "initContainers", "ephemeralContainers"):
        for container in spec.get(category) or []:
            if (container.get("securityContext") or {}).get("privileged") is True:
                settings[f"{category}/{container.get('name')}/privileged"] = True
    for volume in spec.get("volumes") or []:
        if volume.get("hostPath") is not None:
            settings[f"volumes/{volume.get('name')}/hostPath"] = volume["hostPath"]
    return settings


def workload_adds_network_access(resource: str, proposed: dict, current: dict | None = None) -> bool:
    """Allow unchanged restrictions and removals, but reject new permissions."""
    before = workload_network_settings(resource, current or {})
    after = workload_network_settings(resource, proposed)
    return any(key not in before or before[key] != value for key, value in after.items())


def apply_json_patch(current: dict, patch: list) -> dict:
    """Resolve JSON Patch copy/move/test operations against the stored object."""
    result = jsonpatch.apply_patch(current, patch, in_place=False)
    if not isinstance(result, dict):
        raise ValueError("A Kubernetes resource must be an object")
    return result


def merge_object(current: dict, patch: dict) -> dict:
    """Resolve a JSON merge patch for policy fields (RFC 7396)."""
    result = deepcopy(current)
    for key, value in patch.items():
        if value is None:
            result.pop(key, None)
        elif isinstance(value, dict):
            result[key] = merge_object(result.get(key) if isinstance(result.get(key), dict) else {}, value)
        else:
            result[key] = deepcopy(value)
    return result


def policy_tier(value: dict) -> str:
    return (value.get("spec") or {}).get("tier") or "default"
