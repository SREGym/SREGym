"""Immutable runner-private inputs and evidence for the recovery application.

These contracts carry captured facts, not live clients or runtime handles. A
valid object does not establish deployment, calibration, or incident success.
Only ``ScenarioSpec.public_configuration`` may be rendered into the application.
"""

import hashlib
import json
import math
import re
from dataclasses import asdict, dataclass, replace
from enum import StrEnum
from urllib.parse import urlsplit
from uuid import UUID

from sregym.conductor.scenarios.database_recovery import ScaleTier, SeedStreams


def _integer(value: int, name: str, *, minimum: int = 0) -> None:
    if type(value) is not int or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")


def _text(value: str, name: str) -> None:
    if (
        type(value) is not str
        or not value.strip()
        or value != value.strip()
        or len(value) > 256
        or any(ord(c) < 32 for c in value)
    ):
        raise ValueError(f"{name} must be a bounded nonempty string without control characters")


def _dns_name(value: str, name: str) -> None:
    if type(value) is not str or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", value):
        raise ValueError(f"{name} must be a DNS label")


def _digest(value: str, name: str) -> None:
    if type(value) is not str or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")


def _run_id(value: str) -> None:
    _text(value, "run_id")
    try:
        parsed = UUID(value)
    except ValueError as exc:
        raise ValueError("run_id must be a canonical UUID") from exc
    if str(parsed) != value:
        raise ValueError("run_id must be a canonical UUID")


def _tuple(value: tuple, item_type: type, name: str, *, nonempty: bool = False) -> None:
    if type(value) is not tuple or any(type(item) is not item_type for item in value) or (nonempty and not value):
        raise ValueError(f"{name} must be an immutable tuple of {item_type.__name__} objects")


def _unique(values: tuple, name: str) -> None:
    if len(set(values)) != len(values):
        raise ValueError(f"{name} must be unique")


def _origin(value: str, schemes: set[str]) -> None:
    _text(value, "endpoint")
    parsed = urlsplit(value)
    try:
        valid_port = parsed.port is None or 1 <= parsed.port <= 65535
    except ValueError as exc:
        raise ValueError("Endpoint port is invalid") from exc
    if (
        parsed.scheme not in schemes
        or not parsed.hostname
        or not valid_port
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Public endpoints must be credential-free service origins")


class IncidentFamily(StrEnum):
    REGIONAL_FAILOVER = "regional-failover"
    DATABASE_RECOVERY = "database-recovery"


class RecoveryRule(StrEnum):
    BOTH_ACKNOWLEDGED_HISTORIES = "both-acknowledged-histories"
    RETAINED_FLOOR_AND_FRESH_WRITES = "retained-floor-and-fresh-writes"


class LifecyclePhase(StrEnum):
    CREATED = "created"
    PROVISIONING = "provisioning"
    HEALTHY = "healthy"
    BASELINE = "baseline"
    FAULTED = "faulted"
    HANDOFF = "handoff"
    RECOVERING = "recovering"
    VERIFYING = "verifying"
    COMPLETE = "complete"
    INVALID = "invalid"
    STOPPING = "stopping"
    STOPPED = "stopped"


@dataclass(frozen=True)
class DigestEntry:
    name: str
    sha256: str

    def __post_init__(self):
        _text(self.name, "digest name")
        _digest(self.sha256, "sha256")


@dataclass(frozen=True)
class ServiceEndpoint:
    role: str
    origin: str

    def __post_init__(self):
        if type(self.role) is not str or self.role not in {
            "api",
            "gateway",
            "worker",
            "search",
            "queue",
            "repository",
            "artifact",
            "delivery",
            "telemetry",
        }:
            raise ValueError("Unsupported public service role")
        _origin(self.origin, {"http", "https", "amqp", "amqps"})


@dataclass(frozen=True)
class RegionSpec:
    name: str
    namespace: str
    worker_nodes: tuple[str, ...]
    endpoints: tuple[ServiceEndpoint, ...]

    def __post_init__(self):
        if type(self.name) is not str or not re.fullmatch(r"region-[a-z]", self.name):
            raise ValueError("Region name must be region-a through region-z")
        _dns_name(self.namespace, "namespace")
        _tuple(self.worker_nodes, str, "worker_nodes", nonempty=True)
        for node in self.worker_nodes:
            _dns_name(node, "worker node")
        _unique(self.worker_nodes, "worker nodes")
        _tuple(self.endpoints, ServiceEndpoint, "endpoints", nonempty=True)
        _unique(tuple((item.role, item.origin) for item in self.endpoints), "region endpoints")


@dataclass(frozen=True)
class DatabaseMember:
    name: str
    region: str
    role: str
    origin: str

    def __post_init__(self):
        _dns_name(self.name, "database member")
        if type(self.region) is not str or not re.fullmatch(r"region-[a-z]", self.region):
            raise ValueError("Database member region is invalid")
        if type(self.role) is not str or self.role not in {"writer", "candidate", "reader", "standby", "staging"}:
            raise ValueError("Unsupported database role")
        _origin(self.origin, {"mysql", "postgresql"})


@dataclass(frozen=True)
class DatabaseGroupSpec:
    name: str
    engine: str
    members: tuple[DatabaseMember, ...]

    def __post_init__(self):
        _dns_name(self.name, "database group")
        if type(self.engine) is not str or self.engine not in {"mysql", "postgresql"}:
            raise ValueError("Unsupported database engine")
        _tuple(self.members, DatabaseMember, "database members", nonempty=True)
        _unique(tuple(item.name for item in self.members), "database member names")
        _unique(tuple(item.origin for item in self.members), "database member origins")
        if sum(item.role == "writer" for item in self.members) != 1:
            raise ValueError("Initial database topology must have exactly one writer")
        if any(urlsplit(item.origin).scheme != self.engine for item in self.members):
            raise ValueError("Database member origins must use their declared engine")


@dataclass(frozen=True)
class VerificationContract:
    schema_version: int
    family: IncidentFamily
    recovery_rule: RecoveryRule
    required_behaviors: tuple[str, ...]
    stable_seconds: int
    deadline_seconds: int
    receipt_flush_seconds: int
    max_error_rate: float
    max_latency_ms: int
    calibration_sha256: str

    def __post_init__(self):
        for name in ("schema_version", "stable_seconds", "deadline_seconds", "receipt_flush_seconds", "max_latency_ms"):
            _integer(getattr(self, name), name, minimum=1)
        if type(self.family) is not IncidentFamily or type(self.recovery_rule) is not RecoveryRule:
            raise ValueError("Verification family and recovery rule must be typed enum values")
        rules = {
            IncidentFamily.REGIONAL_FAILOVER: RecoveryRule.BOTH_ACKNOWLEDGED_HISTORIES,
            IncidentFamily.DATABASE_RECOVERY: RecoveryRule.RETAINED_FLOOR_AND_FRESH_WRITES,
        }
        if self.recovery_rule != rules[self.family]:
            raise ValueError("Recovery rule does not match the incident family")
        _tuple(self.required_behaviors, str, "required_behaviors", nonempty=True)
        _unique(self.required_behaviors, "required behaviors")
        allowed = {
            "journal-history",
            "current-records",
            "regional-reads",
            "replica-convergence",
            "search",
            "git",
            "builds",
            "deliveries",
            "fresh-writes",
            "loss-report",
        }
        if not set(self.required_behaviors) <= allowed:
            raise ValueError("Unsupported required behavior")
        mandatory = allowed - {"loss-report"}
        if self.family == IncidentFamily.DATABASE_RECOVERY:
            mandatory.add("loss-report")
        if not mandatory <= set(self.required_behaviors):
            raise ValueError(
                "History, current state, fresh writes and independent regional business outcomes are mandatory"
            )
        if self.stable_seconds >= self.deadline_seconds or self.receipt_flush_seconds >= self.deadline_seconds:
            raise ValueError("Observation and receipt flushing must fit their deadline")
        if (
            type(self.max_error_rate) is not float
            or not math.isfinite(self.max_error_rate)
            or not 0 <= self.max_error_rate <= 1
        ):
            raise ValueError("max_error_rate must be a finite rate between zero and one")
        _digest(self.calibration_sha256, "calibration_sha256")


@dataclass(frozen=True)
class ScenarioSpec:
    task_version: int
    schema_version: int
    family: IncidentFamily
    tier: ScaleTier
    seeds: SeedStreams
    regions: tuple[RegionSpec, ...]
    database_groups: tuple[DatabaseGroupSpec, ...]
    source_digests: tuple[DigestEntry, ...]
    image_digests: tuple[DigestEntry, ...]
    attempt_seconds: int
    noise_horizon_seconds: int
    verification: VerificationContract

    def __post_init__(self):
        for name in ("task_version", "schema_version", "attempt_seconds", "noise_horizon_seconds"):
            _integer(getattr(self, name), name, minimum=1)
        if (
            type(self.family) is not IncidentFamily
            or type(self.tier) is not ScaleTier
            or type(self.seeds) is not SeedStreams
        ):
            raise ValueError("Scenario family, tier and seeds must use validated contracts")
        if type(self.verification) is not VerificationContract or self.verification.family != self.family:
            raise ValueError("Scenario verification contract must match its family")
        if self.verification.schema_version != self.schema_version:
            raise ValueError("Scenario and verification schema versions must agree")
        _tuple(self.regions, RegionSpec, "regions", nonempty=True)
        _tuple(self.database_groups, DatabaseGroupSpec, "database_groups", nonempty=True)
        for name in ("source_digests", "image_digests"):
            entries = getattr(self, name)
            _tuple(entries, DigestEntry, name, nonempty=True)
            _unique(tuple(entry.name for entry in entries), name)
        expected_regions = tuple(f"region-{chr(97 + index)}" for index in range(self.tier.regions))
        if tuple(region.name for region in self.regions) != expected_regions:
            raise ValueError("Scenario must declare every tier region in canonical order")
        _unique(tuple(region.namespace for region in self.regions), "region namespaces")
        _unique(tuple(node for region in self.regions for node in region.worker_nodes), "regional node ownership")
        if any(len(region.worker_nodes) != self.tier.worker_nodes_per_region for region in self.regions):
            raise ValueError("Each region must declare its complete worker placement pool")
        if len(self.database_groups) != self.tier.database_groups:
            raise ValueError("Scenario database groups must match its tier")
        _unique(tuple(group.name for group in self.database_groups), "database groups")
        _unique(tuple(member.origin for group in self.database_groups for member in group.members), "database origins")
        if any(member.region not in expected_regions for group in self.database_groups for member in group.members):
            raise ValueError("Database members must belong to declared regions")
        if self.noise_horizon_seconds < self.attempt_seconds + self.verification.deadline_seconds:
            raise ValueError("Noise horizon must cover the full attempt and observation deadline")
        if self.family == IncidentFamily.REGIONAL_FAILOVER:
            for group in self.database_groups:
                expected = {
                    ("region-a", "writer"),
                    ("region-a", "reader"),
                    ("region-b", "candidate"),
                    ("region-b", "reader"),
                }
                if self.tier.regions == 3:
                    expected.add(("region-c", "reader"))
                if (
                    group.engine != "mysql"
                    or len(group.members) != len(expected)
                    or {(m.region, m.role) for m in group.members} != expected
                ):
                    raise ValueError("Regional failover needs the complete MySQL writer, candidate and reader topology")
        elif any(group.engine != "postgresql" for group in self.database_groups):
            raise ValueError("Database recovery family requires PostgreSQL")

    def public_configuration(self) -> dict:
        """Explicit operational allowlist; never derive this through ``asdict``."""
        return {
            "regions": [
                {
                    "name": region.name,
                    "namespace": region.namespace,
                    "worker_nodes": list(region.worker_nodes),
                    "services": [{"role": endpoint.role, "origin": endpoint.origin} for endpoint in region.endpoints],
                    "counts": {role: count for name, role, count in self.tier.public_topology() if name == region.name}
                    | {
                        "database": sum(
                            member.region == region.name for group in self.database_groups for member in group.members
                        )
                    },
                }
                for region in self.regions
            ],
            "database_groups": [
                {
                    "name": group.name,
                    "engine": group.engine,
                    "members": [
                        {"name": member.name, "region": member.region, "role": member.role, "origin": member.origin}
                        for member in group.members
                    ],
                }
                for group in self.database_groups
            ],
        }


@dataclass(frozen=True)
class OwnedResource:
    run_id: str
    kind: str
    namespace: str
    name: str
    uid: str

    def __post_init__(self):
        _run_id(self.run_id)
        if type(self.kind) is not str or self.kind not in {
            "Namespace",
            "Deployment",
            "StatefulSet",
            "Pod",
            "Service",
            "EndpointSlice",
            "PersistentVolumeClaim",
            "Job",
            "Secret",
            "ConfigMap",
            "NetworkPolicy",
            "kind-node",
            "volume",
        }:
            raise ValueError("Unsupported owned resource kind")
        if type(self.namespace) is not str:
            raise ValueError("resource namespace must be a string")
        if self.namespace:
            _dns_name(self.namespace, "resource namespace")
        elif self.kind not in {"Namespace", "kind-node", "volume"}:
            raise ValueError("Namespaced resources need an exact namespace")
        if type(self.name) is not str or len(self.name) > 253:
            raise ValueError("resource name must be a bounded DNS subdomain")
        for label in self.name.split("."):
            _dns_name(label, "resource name")
        _text(self.uid, "resource UID")


_NEXT_PHASES = {
    LifecyclePhase.CREATED: {LifecyclePhase.PROVISIONING},
    LifecyclePhase.PROVISIONING: {LifecyclePhase.HEALTHY},
    LifecyclePhase.HEALTHY: {LifecyclePhase.BASELINE},
    LifecyclePhase.BASELINE: {LifecyclePhase.FAULTED},
    LifecyclePhase.FAULTED: {LifecyclePhase.HANDOFF},
    LifecyclePhase.HANDOFF: {LifecyclePhase.RECOVERING, LifecyclePhase.VERIFYING},
    LifecyclePhase.RECOVERING: {LifecyclePhase.VERIFYING},
    LifecyclePhase.VERIFYING: {LifecyclePhase.RECOVERING, LifecyclePhase.COMPLETE},
    LifecyclePhase.COMPLETE: set(),
    LifecyclePhase.INVALID: set(),
    LifecyclePhase.STOPPING: {LifecyclePhase.STOPPED},
    LifecyclePhase.STOPPED: set(),
}


@dataclass(frozen=True)
class RunInventory:
    run_id: str
    generation: int
    phase: LifecyclePhase = LifecyclePhase.CREATED
    resources: tuple[OwnedResource, ...] = ()
    removed_uids: tuple[str, ...] = ()

    def __post_init__(self):
        _run_id(self.run_id)
        _integer(self.generation, "generation", minimum=1)
        if type(self.phase) is not LifecyclePhase:
            raise ValueError("phase must be a LifecyclePhase")
        _tuple(self.resources, OwnedResource, "resources")
        _tuple(self.removed_uids, str, "removed_uids")
        if any(resource.run_id != self.run_id for resource in self.resources):
            raise ValueError("Cannot claim resources owned by another run")
        _unique(tuple(resource.uid for resource in self.resources), "resource UIDs")
        _unique(tuple((r.kind, r.namespace, r.name) for r in self.resources), "resource identities")
        _unique(self.removed_uids, "removed resource UIDs")
        if not set(self.removed_uids) <= {resource.uid for resource in self.resources}:
            raise ValueError("Removal receipts must refer to owned resources")
        if self.removed_uids and self.phase not in {LifecyclePhase.STOPPING, LifecyclePhase.STOPPED}:
            raise ValueError("Removal receipts are only valid during teardown")
        if self.phase == LifecyclePhase.STOPPED and self.cleanup_pending:
            raise ValueError("A stopped run cannot retain pending owned resources")

    @property
    def cleanup_pending(self) -> tuple[OwnedResource, ...]:
        """UID-qualified candidates; the runtime must still verify ownership before deletion."""
        return tuple(resource for resource in reversed(self.resources) if resource.uid not in self.removed_uids)

    def owns(self, resource: OwnedResource) -> bool:
        return type(resource) is OwnedResource and resource in self.resources and resource.uid not in self.removed_uids

    def with_resource(self, resource: OwnedResource) -> "RunInventory":
        if self.phase not in {LifecyclePhase.CREATED, LifecyclePhase.PROVISIONING}:
            raise ValueError("Owned deployment inventory freezes before healthy preparation")
        return replace(self, resources=self.resources + (resource,))

    def mark_removed(self, resource: OwnedResource) -> "RunInventory":
        if self.phase != LifecyclePhase.STOPPING or not self.owns(resource):
            raise ValueError("Removal needs teardown and the exact owned resource identity")
        return replace(self, removed_uids=self.removed_uids + (resource.uid,))

    def transition(self, phase: LifecyclePhase) -> "RunInventory":
        if type(phase) is not LifecyclePhase:
            raise ValueError("Transition needs a LifecyclePhase")
        if phase == self.phase:
            return self
        allowed = set(_NEXT_PHASES[self.phase])
        if self.phase not in {LifecyclePhase.STOPPING, LifecyclePhase.STOPPED}:
            allowed.add(LifecyclePhase.STOPPING)
            if self.phase != LifecyclePhase.COMPLETE:
                allowed.add(LifecyclePhase.INVALID)
        if phase not in allowed:
            raise ValueError(f"Illegal lifecycle transition: {self.phase} -> {phase}")
        return replace(self, phase=phase)


@dataclass(frozen=True)
class EntityCount:
    entity: str
    tenant: str
    region: str
    count: int

    def __post_init__(self):
        for name in ("entity", "tenant", "region"):
            _text(getattr(self, name), name)
        _integer(self.count, "entity count")


@dataclass(frozen=True)
class StorageFootprint:
    sql_bytes: int
    index_bytes: int
    git_bytes: int
    artifact_bytes: int
    retained_log_bytes: int
    temporary_restore_bytes: int

    def __post_init__(self):
        for name in self.__dataclass_fields__:
            _integer(getattr(self, name), name)


@dataclass(frozen=True)
class SizeDistribution:
    metric: str
    samples: int
    minimum: int
    median: int
    p95: int
    maximum: int

    def __post_init__(self):
        _text(self.metric, "distribution metric")
        _integer(self.samples, "distribution samples", minimum=1)
        for name in ("minimum", "median", "p95", "maximum"):
            _integer(getattr(self, name), name)
        if not self.minimum <= self.median <= self.p95 <= self.maximum:
            raise ValueError("Distribution quantiles must be ordered")


@dataclass(frozen=True)
class RecoverySource:
    name: str
    region: str
    kind: str
    sha256: str
    retained_operation_count: int
    contains_full_payload: bool

    def __post_init__(self):
        _text(self.name, "recovery source name")
        _text(self.region, "recovery source region")
        if type(self.kind) is not str or self.kind not in {
            "database",
            "binlog",
            "wal",
            "backup",
            "queue",
            "search",
            "artifact",
            "git",
            "log",
            "client-cache",
        }:
            raise ValueError("Unsupported recovery source kind")
        _digest(self.sha256, "recovery source digest")
        _integer(self.retained_operation_count, "retained operation count")
        if type(self.contains_full_payload) is not bool:
            raise ValueError("contains_full_payload must be boolean; a digest alone is not recoverable data")


@dataclass(frozen=True)
class OperationCount:
    kind: str
    tenant: str
    project: str | None
    group: str
    region: str
    count: int

    def __post_init__(self):
        for name in ("kind", "tenant", "group", "region"):
            _text(getattr(self, name), name)
        if self.project is not None:
            _text(self.project, "project")
        _integer(self.count, "operation count", minimum=1)


@dataclass(frozen=True)
class DatasetManifest:
    schema_version: int
    entity_counts: tuple[EntityCount, ...]
    operation_count: int
    seed_snapshot_digests: tuple[DigestEntry, ...]
    storage: StorageFootprint
    queue_count: int
    oldest_queue_age_seconds: int
    distributions: tuple[SizeDistribution, ...]
    recovery_sources: tuple[RecoverySource, ...]
    operation_mix: tuple[OperationCount, ...] = ()

    def __post_init__(self):
        _integer(self.schema_version, "manifest schema version", minimum=1)
        for name in ("operation_count", "queue_count", "oldest_queue_age_seconds"):
            _integer(getattr(self, name), name)
        _tuple(self.entity_counts, EntityCount, "entity_counts", nonempty=True)
        _unique(tuple((item.entity, item.tenant, item.region) for item in self.entity_counts), "entity count buckets")
        _tuple(self.seed_snapshot_digests, DigestEntry, "seed_snapshot_digests", nonempty=True)
        _unique(tuple(item.name for item in self.seed_snapshot_digests), "seed snapshot names")
        _tuple(self.distributions, SizeDistribution, "distributions")
        _unique(tuple(item.metric for item in self.distributions), "distribution metrics")
        _tuple(self.recovery_sources, RecoverySource, "recovery_sources")
        _unique(tuple(item.name for item in self.recovery_sources), "recovery source names")
        if type(self.storage) is not StorageFootprint:
            raise ValueError("storage must be a measured StorageFootprint")
        if not self.queue_count and self.oldest_queue_age_seconds:
            raise ValueError("An empty queue cannot have an oldest message age")
        _tuple(self.operation_mix, OperationCount, "operation_mix", nonempty=self.schema_version >= 2)
        _unique(tuple((item.kind, item.tenant, item.project) for item in self.operation_mix), "operation mix buckets")
        if self.operation_mix and sum(item.count for item in self.operation_mix) != self.operation_count:
            raise ValueError("Operation mix must account for the complete accepted operation count")


@dataclass(frozen=True)
class HistoryCut:
    epoch: int
    highest_receipt_sequence: int
    accepted_count: int
    receipts_sha256: str
    protected_keys_sha256: str
    closed_at_ns: int

    def __post_init__(self):
        for name in ("epoch", "highest_receipt_sequence", "accepted_count"):
            _integer(getattr(self, name), name)
        _integer(self.closed_at_ns, "closed_at_ns", minimum=1)
        if self.accepted_count > self.highest_receipt_sequence:
            raise ValueError("A receipt cut cannot contain more acknowledgments than its watermark")
        _digest(self.receipts_sha256, "receipt digest")
        _digest(self.protected_keys_sha256, "protected key digest")


@dataclass(frozen=True)
class EvidenceSnapshot:
    run_id: str
    generation: int
    captured_at_ns: int
    manifest: DatasetManifest
    closed_cuts: tuple[HistoryCut, ...]
    artifact_digests: tuple[DigestEntry, ...]

    def __post_init__(self):
        _run_id(self.run_id)
        _integer(self.generation, "evidence generation", minimum=1)
        _integer(self.captured_at_ns, "captured_at_ns", minimum=1)
        if type(self.manifest) is not DatasetManifest:
            raise ValueError("Evidence needs an immutable captured DatasetManifest")
        _tuple(self.closed_cuts, HistoryCut, "closed_cuts")
        _unique(tuple(cut.epoch for cut in self.closed_cuts), "closed epochs")
        if any(cut.closed_at_ns > self.captured_at_ns for cut in self.closed_cuts):
            raise ValueError("Cannot snapshot an epoch that has not closed")
        if tuple(cut.epoch for cut in self.closed_cuts) != tuple(sorted(cut.epoch for cut in self.closed_cuts)):
            raise ValueError("Closed epochs must be in canonical order")
        if tuple(cut.highest_receipt_sequence for cut in self.closed_cuts) != tuple(
            sorted(cut.highest_receipt_sequence for cut in self.closed_cuts)
        ):
            raise ValueError("Closed receipt watermarks must be monotonic")
        _tuple(self.artifact_digests, DigestEntry, "artifact_digests", nonempty=True)
        _unique(tuple(entry.name for entry in self.artifact_digests), "evidence artifact names")

    @property
    def sha256(self) -> str:
        """Canonical identity of captured DTOs; validate artifact bytes separately."""
        encoded = json.dumps(asdict(self), sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        return hashlib.sha256(encoded).hexdigest()
