"""Private scale and seed contracts for the database-recovery family.

These primitives describe real work a future executor must perform. Generating
a schedule neither performs noise nor qualifies a runnable incident task.
"""

import hashlib
import random
from dataclasses import dataclass


def _positive_integer(value: int, name: str) -> None:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name} must be a positive integer")


@dataclass(frozen=True)
class ScaleTier:
    name: str
    zones: int
    api_per_zone: int
    workers_per_zone: int
    search_per_zone: int
    tenants_per_zone: int
    records: int
    cpu_limit: int
    memory_gib_limit: int
    disk_gib_limit: int
    worker_nodes_per_region: int = 3
    database_groups: int = 1

    def __post_init__(self):
        if type(self.name) is not str or self.name not in {"small", "medium", "large"}:
            raise ValueError("Unsupported scale tier")
        for name in (
            "zones",
            "api_per_zone",
            "workers_per_zone",
            "search_per_zone",
            "tenants_per_zone",
            "records",
            "cpu_limit",
            "memory_gib_limit",
            "disk_gib_limit",
            "worker_nodes_per_region",
            "database_groups",
        ):
            _positive_integer(getattr(self, name), name)
        if self.zones > 26:
            raise ValueError("At most 26 logical zones are supported")

    @property
    def regions(self) -> int:
        """Keep the earlier zones field compatible while using regional terminology."""
        return self.zones

    def admit(
        self, *, physical_cores: int, available_memory_gib: int, available_disk_gib: int, reserve_memory_gib: int = 16
    ) -> None:
        """Reserve host headroom and trusted-control capacity before provisioning."""
        _positive_integer(physical_cores, "physical_cores")
        _positive_integer(available_memory_gib, "available_memory_gib")
        _positive_integer(reserve_memory_gib, "reserve_memory_gib")
        _positive_integer(available_disk_gib, "available_disk_gib")
        if self.cpu_limit > physical_cores * 3 // 4:
            raise ValueError("Workload exceeds the physical CPU headroom budget")
        if self.memory_gib_limit > available_memory_gib * 3 // 4 - reserve_memory_gib:
            raise ValueError("Workload exceeds the memory headroom budget")
        if self.disk_gib_limit > available_disk_gib * 3 // 4 - 20:
            raise ValueError("Workload exceeds the disk headroom budget")

    def public_topology(self) -> tuple[tuple[str, str, int], ...]:
        """Normal operational roles/counts only; no fault choices or private seeds."""
        return tuple(
            (f"region-{chr(97 + zone)}", role, count)
            for zone in range(self.zones)
            for role, count in (
                ("api", self.api_per_zone),
                ("worker", self.workers_per_zone),
                ("search", self.search_per_zone),
                ("gateway", 2),
                ("queue", 3),
                ("repository", 1),
                ("artifact", 1),
                ("delivery", 1),
                ("telemetry", 1),
                ("database", self.database_groups * (2 if zone < 2 else 1)),
            )
        )

    def noise_targets(self) -> tuple[str, ...]:
        """Tenant activity scales with the environment, not the selected fault."""
        return tuple(
            f"region-{chr(97 + zone)}/tenant-{tenant:03d}"
            for zone in range(self.zones)
            for tenant in range(self.tenants_per_zone)
        )


TIERS = {
    "small": ScaleTier("small", 2, 2, 2, 1, 4, 20_000, 16, 32, 40, 3, 1),
    "medium": ScaleTier("medium", 2, 4, 4, 2, 8, 200_000, 32, 96, 100, 3, 2),
    "large": ScaleTier("large", 3, 8, 8, 4, 32, 2_000_000, 48, 160, 220, 3, 4),
}


@dataclass(frozen=True)
class SeedStreams:
    topology: int
    data: int
    noise: int
    fault: int

    def __post_init__(self):
        for name in ("topology", "data", "noise", "fault"):
            if type(getattr(self, name)) is not int or getattr(self, name) < 0:
                raise ValueError(f"{name} seed must be a nonnegative integer")

    @classmethod
    def derive(cls, root_seed: int) -> "SeedStreams":
        if type(root_seed) is not int or root_seed < 0:
            raise ValueError("root_seed must be a nonnegative integer")
        return cls(
            **{
                domain: int.from_bytes(hashlib.sha256(f"recovery-v1/{domain}/{root_seed}".encode()).digest(), "big")
                for domain in ("topology", "data", "noise", "fault")
            }
        )


@dataclass(frozen=True)
class NoiseEvent:
    at_second: int
    duration_seconds: int
    target: str
    operation: str

    def __post_init__(self):
        if type(self.at_second) is not int or self.at_second < 0:
            raise ValueError("at_second must be a nonnegative integer")
        _positive_integer(self.duration_seconds, "duration_seconds")
        if type(self.target) is not str or not self.target:
            raise ValueError("target must be a nonempty string")
        if type(self.operation) is not str or self.operation not in {
            "traffic-burst",
            "index-refresh",
            "worker-recycle",
        }:
            raise ValueError("Unsupported noise operation")


@dataclass(frozen=True)
class NoiseDecision:
    """An admission decision; this is deliberately not an execution receipt."""

    event_id: str
    event: NoiseEvent
    admitted: bool
    rejection_reason: str | None = None

    def __post_init__(self):
        if type(self.event_id) is not str or not self.event_id:
            raise ValueError("event_id must be a nonempty string")
        if type(self.event) is not NoiseEvent or type(self.admitted) is not bool:
            raise ValueError("A decision needs a NoiseEvent and boolean admission")
        if self.admitted and self.rejection_reason is not None:
            raise ValueError("Admitted opportunities cannot have a rejection reason")
        if not self.admitted and (
            type(self.rejection_reason) is not str
            or self.rejection_reason not in {"noise-disabled", "target-busy", "global-cap"}
        ):
            raise ValueError("Rejected opportunities must retain a supported reason")


@dataclass(frozen=True)
class NoisePlan:
    scheduler_version: str
    decisions: tuple[NoiseDecision, ...]

    def __post_init__(self):
        if self.scheduler_version != "recovery-noise-v2":
            raise ValueError("Unsupported noise scheduler version")
        if type(self.decisions) is not tuple or any(type(item) is not NoiseDecision for item in self.decisions):
            raise ValueError("decisions must be an immutable tuple of NoiseDecision objects")
        if len({item.event_id for item in self.decisions}) != len(self.decisions):
            raise ValueError("Noise event IDs must be unique")

    @property
    def requested(self) -> tuple[NoiseEvent, ...]:
        return tuple(item.event for item in self.decisions)

    @property
    def admitted(self) -> tuple[NoiseEvent, ...]:
        return tuple(item.event for item in self.decisions if item.admitted)

    @property
    def rejected(self) -> tuple[NoiseDecision, ...]:
        return tuple(item for item in self.decisions if not item.admitted)

    def counts_by_target(self) -> tuple[tuple[str, int, int, int], ...]:
        """Requested/admitted/rejected counts, never mislabeled as actual dose."""
        return tuple(
            (
                target,
                sum(item.event.target == target for item in self.decisions),
                sum(item.event.target == target and item.admitted for item in self.decisions),
                sum(item.event.target == target and not item.admitted for item in self.decisions),
            )
            for target in sorted({item.event.target for item in self.decisions})
        )


def plan_noise(
    tier: ScaleTier,
    seed: int,
    *,
    horizon_seconds: int,
    interval_seconds: int = 90,
    duration_seconds: int = 15,
    max_concurrent: int = 2,
    enabled: bool = True,
) -> NoisePlan:
    """Plan bounded real actions independently of injection/submission timing.

    Opportunities grow per tenant; execution is capped to protect the host.
    Rejected overlapping opportunities are not silently recorded as executed.
    """
    if type(seed) is not int or seed < 0:
        raise ValueError("noise seed must be a nonnegative integer")
    if type(enabled) is not bool:
        raise ValueError("enabled must be a boolean")
    for name, value in (
        ("horizon_seconds", horizon_seconds),
        ("interval_seconds", interval_seconds),
        ("duration_seconds", duration_seconds),
        ("max_concurrent", max_concurrent),
    ):
        _positive_integer(value, name)
    if duration_seconds >= interval_seconds or duration_seconds > horizon_seconds:
        raise ValueError("Noise duration must fit the horizon and remain shorter than its interval")
    if type(tier) is not ScaleTier:
        raise ValueError("tier must be a ScaleTier")
    rng = random.Random(seed)  # nosec B311: simulation scheduling, not credentials
    candidates = []
    for target in tier.noise_targets():
        offset = rng.randrange(interval_seconds)
        for at in range(offset, horizon_seconds - duration_seconds + 1, interval_seconds):
            candidates.append(
                NoiseEvent(
                    at,
                    duration_seconds,
                    target,
                    rng.choice(
                        (
                            "traffic-burst",
                            "index-refresh",
                            "worker-recycle",
                        )
                    ),
                )
            )
    rng.shuffle(candidates)
    candidates.sort(key=lambda event: event.at_second)
    decisions = []
    active = []
    for index, event in enumerate(candidates):
        active = [previous for previous in active if previous.at_second + previous.duration_seconds > event.at_second]
        reason = None
        if not enabled:
            reason = "noise-disabled"
        elif any(previous.target == event.target for previous in active):
            reason = "target-busy"
        elif len(active) >= max_concurrent:
            reason = "global-cap"
        event_id = hashlib.sha256(
            f"recovery-noise-v2/{seed}/{index}/{event.at_second}/{event.target}/{event.operation}".encode()
        ).hexdigest()
        decisions.append(NoiseDecision(event_id, event, reason is None, reason))
        if reason is None:
            active.append(event)
    return NoisePlan("recovery-noise-v2", tuple(decisions))


def noise_schedule(tier: ScaleTier, seed: int, **options) -> tuple[NoiseEvent, ...]:
    """Compatibility view of admitted events; no action has been executed here."""
    return plan_noise(tier, seed, **options).admitted
