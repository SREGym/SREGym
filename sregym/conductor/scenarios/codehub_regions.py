"""Runner-private placement and endpoint inventory for ordinary logical regions."""

import re
from collections.abc import Mapping

from sregym.conductor.scenarios.codehub_contracts import (
    DatabaseGroupSpec,
    DatabaseMember,
    RegionSpec,
    ServiceEndpoint,
)
from sregym.conductor.scenarios.database_recovery import ScaleTier

REGION_LABEL = "topology.kubernetes.io/region"
ZONE_LABEL = "topology.kubernetes.io/zone"


def kind_configuration(tier: ScaleTier, *, node_image: str) -> dict:
    """Describe rootless Kind placement; cluster creation belongs to the runner."""
    if not re.fullmatch(r"[^\s]+@sha256:[0-9a-f]{64}", node_image):
        raise ValueError("Kind node image must have a resolved content digest")
    return {
        "kind": "Cluster",
        "apiVersion": "kind.x-k8s.io/v1alpha4",
        "nodes": [{"role": "control-plane", "image": node_image}]
        + [
            {
                "role": "worker",
                "image": node_image,
                "labels": {
                    REGION_LABEL: f"region-{chr(97 + region)}",
                    ZONE_LABEL: f"region-{chr(97 + region)}-node-{node}",
                },
            }
            for region in range(tier.regions)
            for node in range(tier.worker_nodes_per_region)
        ],
    }


def regional_inventory(tier: ScaleTier, node_labels: Mapping[str, Mapping[str, str]]) -> tuple[RegionSpec, ...]:
    """Require distinct actual nodes and placement zones for every declared region."""
    regions = []
    for index in range(tier.regions):
        region = f"region-{chr(97 + index)}"
        nodes = tuple(sorted(name for name, labels in node_labels.items() if labels.get(REGION_LABEL) == region))
        if len(nodes) != tier.worker_nodes_per_region:
            raise ValueError(f"{region} requires exactly {tier.worker_nodes_per_region} labeled worker nodes")
        zones = tuple(node_labels[node].get(ZONE_LABEL) for node in nodes)
        if None in zones or "" in zones or len(set(zones)) != len(zones):
            raise ValueError(f"{region} requires distinct nonempty placement zones")
        namespace = f"codehub-{region}"
        regions.append(
            RegionSpec(
                region,
                namespace,
                nodes,
                tuple(
                    ServiceEndpoint(role, f"http://{role}.{namespace}.svc.cluster.local:8080")
                    for role in ("gateway", "api", "repository", "search", "delivery", "artifact")
                )
                + (
                    ServiceEndpoint("queue", f"amqp://queue.{namespace}.svc.cluster.local:5672"),
                    ServiceEndpoint("telemetry", f"http://telemetry.{namespace}.svc.cluster.local:4318"),
                ),
            )
        )
    return tuple(regions)


def mysql_groups(tier: ScaleTier, regions: tuple[RegionSpec, ...]) -> tuple[DatabaseGroupSpec, ...]:
    if tuple(region.name for region in regions) != tuple(f"region-{chr(97 + i)}" for i in range(tier.regions)):
        raise ValueError("Database placement needs the complete canonical region inventory")
    return tuple(
        DatabaseGroupSpec(
            f"group-{group}",
            "mysql",
            tuple(
                DatabaseMember(
                    f"mysql-g{group}-{region.name[-1]}-{role}",
                    region.name,
                    role,
                    f"mysql://mysql-g{group}-{role}.{region.namespace}.svc.cluster.local:3306",
                )
                for region in regions
                for role in (
                    ("writer", "reader")
                    if region.name == "region-a"
                    else ("candidate", "reader")
                    if region.name == "region-b"
                    else ("reader",)
                )
            ),
        )
        for group in range(tier.database_groups)
    )


def region_values(tier: ScaleTier, region: RegionSpec, groups: tuple[DatabaseGroupSpec, ...]) -> dict:
    """Render operational settings without private difficulty, fault, or seed fields."""
    first_writer = next(member for member in groups[0].members if member.role == "writer")
    values = {
        "region": region.name,
        "regions": [f"region-{chr(97 + i)}" for i in range(tier.regions)],
        "replicas": {"api": tier.api_per_zone, "worker": tier.workers_per_zone, "search": tier.search_per_zone},
        "mysql": {
            "primaryHost": first_writer.origin.removeprefix("mysql://").rsplit(":", 1)[0],
            "readHost": f"mysql-g0-reader.{region.namespace}.svc.cluster.local",
            "instances": [
                {
                    "name": member.origin.split("//", 1)[1].split(".", 1)[0],
                    "serverId": group_index * 10 + ordinal + 1,
                    "writable": member.role == "writer",
                }
                for group_index, group in enumerate(groups)
                for ordinal, member in enumerate(group.members)
                if member.region == region.name
            ],
        },
        "quota": {
            "cpu": str(tier.cpu_limit // tier.regions),
            "memory": f"{tier.memory_gib_limit // tier.regions}Gi",
            "storage": f"{tier.disk_gib_limit // tier.regions}Gi",
        },
    }
    if tier.api_per_zone > 4:
        values["mysql"]["resources"] = {
            "requests": {"cpu": "100m", "memory": "512Mi"},
            "limits": {"cpu": "750m", "memory": "2Gi"},
        }
        values["services"] = {
            role: {
                "resources": {
                    "requests": {"cpu": "50m", "memory": "64Mi"},
                    "limits": {"cpu": "250m", "memory": "1Gi" if role == "search" else "512Mi"},
                }
            }
            for role in ("api", "worker", "search")
        }
    return values
