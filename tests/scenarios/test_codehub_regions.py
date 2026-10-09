import json
import shutil
import subprocess
from pathlib import Path

import pytest

from sregym.conductor.scenarios.codehub_regions import (
    REGION_LABEL,
    ZONE_LABEL,
    kind_configuration,
    mysql_groups,
    region_values,
    regional_inventory,
)
from sregym.conductor.scenarios.database_recovery import TIERS


def node_labels(tier):
    return {
        f"worker-{region}-{node}": {REGION_LABEL: f"region-{chr(97 + region)}", ZONE_LABEL: f"zone-{region}-{node}"}
        for region in range(tier.regions)
        for node in range(tier.worker_nodes_per_region)
    }


@pytest.mark.parametrize("name", ["small", "medium", "large"])
def test_complete_regions_and_real_mysql_member_inventory(name):
    tier = TIERS[name]
    regions = regional_inventory(tier, node_labels(tier))
    groups = mysql_groups(tier, regions)
    assert len(regions) == tier.regions
    assert len({node for region in regions for node in region.worker_nodes}) == tier.regions * 3
    assert all(len(region.worker_nodes) == 3 for region in regions)
    assert all(
        {endpoint.role for endpoint in region.endpoints}
        >= {"gateway", "repository", "artifact", "search", "queue", "telemetry"}
        for region in regions
    )
    assert len(groups) == tier.database_groups
    for group in groups:
        assert sum(member.role == "writer" for member in group.members) == 1
        assert len({member.origin for member in group.members}) == (5 if tier.regions == 3 else 4)


@pytest.mark.parametrize("change", ["missing-node", "duplicate-zone", "missing-zone"])
def test_namespace_only_or_invalid_node_placement_is_rejected(change):
    tier = TIERS["small"]
    labels = node_labels(tier)
    if change == "missing-node":
        labels.pop("worker-0-0")
    elif change == "duplicate-zone":
        labels["worker-0-0"][ZONE_LABEL] = labels["worker-0-1"][ZONE_LABEL]
    else:
        labels["worker-0-0"].pop(ZONE_LABEL)
    with pytest.raises(ValueError):
        regional_inventory(tier, labels)


def test_kind_description_separates_control_plane_and_six_worker_domains():
    config = kind_configuration(TIERS["small"], node_image="kindest/node@sha256:" + "a" * 64)
    assert len(config["nodes"]) == 7
    assert config["nodes"][0]["role"] == "control-plane"
    assert "labels" not in config["nodes"][0]
    assert all(node["role"] == "worker" for node in config["nodes"][1:])
    assert len({node["labels"][ZONE_LABEL] for node in config["nodes"][1:]}) == 6
    with pytest.raises(ValueError, match="resolved"):
        kind_configuration(TIERS["small"], node_image="kindest/node:latest")


@pytest.mark.parametrize("name", ["small", "medium", "large"])
def test_public_values_have_unique_server_ids_and_no_private_scenario_fields(name):
    tier = TIERS[name]
    regions = regional_inventory(tier, node_labels(tier))
    groups = mysql_groups(tier, regions)
    values = [region_values(tier, region, groups) for region in regions]
    server_ids = [member["serverId"] for region in values for member in region["mysql"]["instances"]]
    assert len(server_ids) == len(set(server_ids))
    encoded = json.dumps(values)
    assert all(private not in encoded for private in ("task_id", "family", "seed", "fault", "verification", "tier"))
    assert all(region["mysql"]["primaryHost"].startswith("mysql-g0-writer.") for region in values)


@pytest.mark.parametrize("name", ["small", "medium", "large"])
def test_rendered_replica_placement_is_independent_of_unrelated_service_load(name, tmp_path):
    import yaml

    if shutil.which("helm") is None:
        pytest.skip("Actual Helm renderer is required")
    tier = TIERS[name]
    regions = regional_inventory(tier, node_labels(tier))
    groups = mysql_groups(tier, regions)
    chart = Path(__file__).resolve().parents[2] / "SREGym-applications/codehub/helm"
    for region in regions:
        values = tmp_path / "values.yaml"
        values.write_text(yaml.safe_dump(region_values(tier, region, groups)), encoding="utf-8")
        rendered = subprocess.check_output(
            ["helm", "template", "customer-services", str(chart), "-f", str(values)], text=True, timeout=20
        )
        controllers = [
            item for item in yaml.safe_load_all(rendered) if item and item["kind"] in {"Deployment", "StatefulSet"}
        ]
        for controller in controllers:
            pod = controller["spec"]["template"]
            labels, spec = pod["metadata"]["labels"], pod["spec"]
            if labels["app.kubernetes.io/component"] in {"api", "repository"}:
                (container,) = spec["containers"]
                assert container["readinessProbe"]["httpGet"]["path"] == "/readyz"
                assert 1 < container["readinessProbe"]["timeoutSeconds"] <= 5
                assert container["livenessProbe"]["httpGet"]["path"] == "/healthz"
                assert container["livenessProbe"].get("timeoutSeconds", 1) == 1
            assert spec["nodeSelector"] == {REGION_LABEL: region.name}
            (constraint,) = spec["topologySpreadConstraints"]
            assert constraint["whenUnsatisfiable"] == "DoNotSchedule"
            assert constraint["topologyKey"] == "kubernetes.io/hostname" and constraint["maxSkew"] == 1
            selector = constraint["labelSelector"]["matchLabels"]

            def matches(candidate, selector=selector):
                return all(candidate.get(key) == value for key, value in selector.items())

            assert matches(labels)
            assert not matches({**labels, "app.kubernetes.io/instance": "another-release"})
            assert not matches({**labels, "app.kubernetes.io/component": "another-service"})
            if labels["app.kubernetes.io/component"] not in {"queue", "mysql"}:
                continue
            (anti,) = spec["affinity"]["podAntiAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"]
            anti_selector = anti["labelSelector"]["matchLabels"]
            assert anti["topologyKey"] == "kubernetes.io/hostname"
            if labels["app.kubernetes.io/component"] == "mysql":
                assert not matches({**labels, "database-group": "another-group"})
                assert not matches({**labels, "database-group": "another-group"}, anti_selector)

            # Reproduce the observed deadlock: unrelated services fill node 0,
            # while existing replicas occupy the other eligible nodes. The
            # final replica must still be admitted on its distinct free node.
            occupied = [(0, {**labels, "app.kubernetes.io/component": "another-service"})] * 2
            occupied += [(1, labels)]
            if labels["app.kubernetes.io/component"] == "queue":
                occupied += [(2, labels)]
            else:
                occupied += [
                    (2, {**labels, "database-group": "another-group"}),
                    (2, {**labels, "app.kubernetes.io/component": "another-service"}),
                ]
            free = [
                node
                for node in range(3)
                if not any(
                    existing_node == node and all(existing.get(key) == value for key, value in anti_selector.items())
                    for existing_node, existing in occupied
                )
            ]
            counts = [
                sum(node == existing_node and matches(existing) for existing_node, existing in occupied)
                for node in range(3)
            ]
            assert 0 in free and counts[0] + 1 - min(counts) <= constraint["maxSkew"]
            original_counts = [
                sum(
                    node == existing_node and existing.get("app.kubernetes.io/name") == "codehub"
                    for existing_node, existing in occupied
                )
                for node in range(3)
            ]
            assert not any(original_counts[node] + 1 - min(original_counts) <= constraint["maxSkew"] for node in free)


def test_worker_probes_allow_bounded_python_startup_without_relaxing_heartbeat_age(tmp_path):
    import yaml

    if shutil.which("helm") is None:
        pytest.skip("Actual Helm renderer is required")
    chart = Path(__file__).resolve().parents[2] / "SREGym-applications/codehub/helm"
    rendered = subprocess.check_output(["helm", "template", "codehub", str(chart)], text=True, timeout=20)
    worker = next(
        item
        for item in yaml.safe_load_all(rendered)
        if item and item["kind"] == "Deployment" and item["metadata"]["name"] == "worker"
    )
    (container,) = worker["spec"]["template"]["spec"]["containers"]
    for probe, maximum_age in (("readinessProbe", 60), ("livenessProbe", 90)):
        assert 1 < container[probe]["timeoutSeconds"] <= 5
        assert f"< {maximum_age}" in container[probe]["exec"]["command"][-1]
