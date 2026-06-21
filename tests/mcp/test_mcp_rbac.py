from pathlib import Path

import yaml

CLUSTERROLE = Path(__file__).resolve().parents[2] / "mcp_server" / "k8s" / "clusterrole.yaml"


def _rules():
    return yaml.safe_load(CLUSTERROLE.read_text())["rules"]


def test_mcp_rbac_allows_calico_bgp_repair_resources():
    calico_rule = next(rule for rule in _rules() if "crd.projectcalico.org" in rule["apiGroups"])

    assert "bgppeers" in calico_rule["resources"]
    assert "bgpconfigurations" in calico_rule["resources"]
    assert {"get", "list", "watch", "patch", "update", "create", "delete"}.issubset(set(calico_rule["verbs"]))


def test_mcp_rbac_allows_node_label_and_annotation_repair():
    node_rule = next(
        rule
        for rule in _rules()
        if rule["apiGroups"] == [""] and "nodes" in rule["resources"] and "patch" in rule["verbs"]
    )

    assert {"get", "list", "watch", "patch", "update"}.issubset(set(node_rule["verbs"]))


def test_mcp_rbac_allows_endpoint_slice_read_access():
    discovery_rule = next(rule for rule in _rules() if "discovery.k8s.io" in rule["apiGroups"])

    assert "endpointslices" in discovery_rule["resources"]
    assert set(discovery_rule["verbs"]) == {"get", "list", "watch"}
