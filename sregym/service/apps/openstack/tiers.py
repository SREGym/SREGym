"""Scale tiers for the OpenStack region.

A tier changes the size of the region (racks, hypervisors, tenants, resident
instances) and the tenant load on it. It never changes the services in the
control plane, so an incident family keeps the same causal chain at every tier.
Hypervisors run Nova's fake virt driver, so a tier's cost grows with the number
of nova-compute processes (about 130 MiB each), not with guest memory.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CloudTier:
    name: str
    racks: tuple[str, ...]
    hosts_per_rack: int
    tenants: int
    #: Long-lived customer instances per tenant, booted before the incident.
    pets_per_tenant: int
    #: Seconds a tenant waits between operations.
    think_time_s: float = 2.0
    #: Hypervisors already disabled for hardware maintenance when the incident
    #: starts. They are part of the healthy baseline, not of any fault.
    maintenance_hosts: tuple[str, ...] = ()
    #: Object storage (when a family enables it): one storage node per rack,
    #: each rack a Swift zone, with this many devices per node.
    swift_devices_per_node: int = 2
    swift_part_power: int = 8
    #: Objects each tenant stores before the incident; their content is derived
    #: from their names, so any copy can be verified.
    canaries_per_tenant: int = 24

    @property
    def storage_nodes(self) -> list[str]:
        return [f"swift-storage-{index}" for index in range(len(self.racks))]

    @property
    def hypervisors(self) -> list[str]:
        return [hypervisor_name(rack, index) for rack in self.racks for index in range(self.hosts_per_rack)]

    @property
    def pets(self) -> int:
        return self.tenants * self.pets_per_tenant

    def rack_of(self, host: str) -> str:
        for rack in self.racks:
            if host.startswith(f"hv-{rack}-"):
                return rack
        raise ValueError(f"{host!r} is not a hypervisor of tier {self.name}")


def hypervisor_name(rack: str, index: int) -> str:
    # StatefulSet pod names double as the nova-compute host names.
    return f"hv-{rack}-{index}"


TIERS = {
    # One small region: enough racks that losing one is partial, not total.
    "small": CloudTier("small", ("r1", "r2", "r3"), hosts_per_rack=2, tenants=3, pets_per_tenant=4),
    # A regional tier: more racks, deeper inventory, more resident state.
    "regional": CloudTier(
        "regional",
        ("r1", "r2", "r3", "r4"),
        hosts_per_rack=4,
        tenants=6,
        pets_per_tenant=5,
        maintenance_hosts=("hv-r1-1",),
        swift_devices_per_node=3,
        swift_part_power=9,
        canaries_per_tenant=32,
    ),
    # Fleet tier for a CloudLab-class host (about 10 GiB for hypervisors alone).
    "fleet": CloudTier(
        "fleet",
        ("r1", "r2", "r3", "r4", "r5", "r6"),
        hosts_per_rack=8,
        tenants=10,
        pets_per_tenant=6,
        think_time_s=1.0,
        maintenance_hosts=("hv-r2-3", "hv-r6-0"),
        swift_devices_per_node=4,
        swift_part_power=10,
        canaries_per_tenant=40,
    ),
}
