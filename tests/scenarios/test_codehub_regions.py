import json

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
