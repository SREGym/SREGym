"""Private contract checks for ownership, captured evidence and public visibility."""

import json
from dataclasses import FrozenInstanceError, replace
from uuid import uuid4

import pytest

from sregym.conductor.scenarios.codehub_contracts import (
    DatabaseGroupSpec,
    DatabaseMember,
    DatasetManifest,
    DigestEntry,
    EntityCount,
    EvidenceSnapshot,
    HistoryCut,
    IncidentFamily,
    LifecyclePhase,
    OwnedResource,
    RecoveryRule,
    RecoverySource,
    RegionSpec,
    RunInventory,
    ScenarioSpec,
    ServiceEndpoint,
    SizeDistribution,
    StorageFootprint,
    VerificationContract,
)
from sregym.conductor.scenarios.database_recovery import TIERS, SeedStreams

HASH = "a" * 64
BEHAVIORS = (
    "journal-history",
    "current-records",
    "regional-reads",
    "replica-convergence",
    "search",
    "git",
    "builds",
    "deliveries",
    "fresh-writes",
)


def contract():
    return VerificationContract(
        1,
        IncidentFamily.REGIONAL_FAILOVER,
        RecoveryRule.BOTH_ACKNOWLEDGED_HISTORIES,
        BEHAVIORS,
        60,
        600,
        15,
        0.01,
        1000,
        HASH,
    )


def scenario():
    regions = tuple(
        RegionSpec(
            f"region-{letter}",
            f"platform-{letter}",
            tuple(f"node-{letter}-{n}" for n in range(3)),
            (ServiceEndpoint("api", f"https://api.platform-{letter}:8080"),),
        )
        for letter in "ab"
    )
    members = tuple(
        DatabaseMember(f"db-{letter}-{role}", f"region-{letter}", role, f"mysql://db-{letter}-{role}:3306")
        for letter, role in (("a", "writer"), ("a", "reader"), ("b", "candidate"), ("b", "reader"))
    )
    return ScenarioSpec(
        1,
        1,
        IncidentFamily.REGIONAL_FAILOVER,
        TIERS["small"],
        SeedStreams.derive(1919),
        regions,
        (DatabaseGroupSpec("group-a", "mysql", members),),
        (DigestEntry("application", HASH),),
        (DigestEntry("database", "b" * 64),),
        1800,
        2400,
        contract(),
    )


def manifest():
    return DatasetManifest(
        1,
        (EntityCount("projects", "tenant-000", "region-a", 10),),
        200,
        (DigestEntry("seed-snapshot", HASH),),
        StorageFootprint(1000, 100, 2000, 3000, 400, 5000),
        4,
        20,
        (SizeDistribution("git-object-bytes", 20, 10, 25, 75, 100),),
        (RecoverySource("writer-binlog", "region-a", "binlog", HASH, 200, True),),
    )


def inventory():
    run_id = str(uuid4())
    resource = OwnedResource(run_id, "StatefulSet", "platform-a", "db-a-writer", str(uuid4()))
    return RunInventory(run_id, 1, resources=(resource,)), resource


def test_public_configuration_is_an_explicit_operational_allowlist_and_detached_copy():
    spec = scenario()
    public = spec.public_configuration()
    assert set(public) == {"regions", "database_groups"}
    assert set(public["regions"][0]) == {"name", "namespace", "worker_nodes", "services", "counts"}
    encoded = json.dumps(public)
    for forbidden in ("seeds", "task_version", "family", "calibration_sha256", "image_digests", "fault", HASH):
        assert forbidden not in encoded
    assert str(spec.seeds.data) not in encoded
    assert "mysql" in encoded and "writer" in encoded and "reader" in encoded
    public["regions"][0]["worker_nodes"].clear()
    assert len(spec.public_configuration()["regions"][0]["worker_nodes"]) == 3
    with pytest.raises(FrozenInstanceError):
        spec.regions[0].namespace = "changed"


@pytest.mark.parametrize(
    "origin",
    [
        "https://user:password@api",
        "https://api?seed=42",
        "https://api/private-repair",
        "https://api#token",
        "file:///etc/passwd",
        "https://api:65536",
    ],
)
def test_public_endpoint_cannot_carry_credentials_private_routes_or_local_files(origin):
    with pytest.raises(ValueError):
        ServiceEndpoint("api", origin)


@pytest.mark.parametrize("field,value", [("worker_nodes", ["node-a-0"]), ("endpoints", []), ("namespace", True)])
def test_region_cannot_hide_mutable_runtime_handles_or_invalid_names(field, value):
    with pytest.raises(ValueError):
        replace(scenario().regions[0], **{field: value})


def test_complete_region_and_database_inventory_is_required_before_provisioning():
    spec = scenario()
    with pytest.raises(ValueError, match="every tier region"):
        replace(spec, regions=spec.regions[:1])
    with pytest.raises(ValueError, match="placement pool"):
        replace(
            spec, regions=(replace(spec.regions[0], worker_nodes=spec.regions[0].worker_nodes[:2]), spec.regions[1])
        )
    with pytest.raises(ValueError, match="complete MySQL"):
        replace(spec, database_groups=(replace(spec.database_groups[0], members=spec.database_groups[0].members[:3]),))
    with pytest.raises(ValueError, match="observation deadline"):
        replace(spec, noise_horizon_seconds=1800)


def test_shared_public_topology_reports_actual_postgresql_members_without_mysql_assumptions():
    spec = scenario()
    members = tuple(
        DatabaseMember(f"db-{letter}-{role}", f"region-{letter}", role, f"postgresql://db-{letter}-{role}:5432")
        for letter, role in (("a", "writer"), ("b", "standby"), ("b", "staging"))
    )
    recovery = replace(
        contract(),
        family=IncidentFamily.DATABASE_RECOVERY,
        recovery_rule=RecoveryRule.RETAINED_FLOOR_AND_FRESH_WRITES,
        required_behaviors=BEHAVIORS + ("loss-report",),
    )
    shared = replace(
        spec,
        family=IncidentFamily.DATABASE_RECOVERY,
        verification=recovery,
        database_groups=(DatabaseGroupSpec("group-a", "postgresql", members),),
    )
    regions = shared.public_configuration()["regions"]
    assert [region["counts"]["database"] for region in regions] == [1, 2]


def test_regions_and_database_endpoints_cannot_claim_shared_placement_or_storage_identity():
    spec = scenario()
    with pytest.raises(ValueError, match="node ownership"):
        replace(spec, regions=(spec.regions[0], replace(spec.regions[1], worker_nodes=spec.regions[0].worker_nodes)))
    with pytest.raises(ValueError, match="namespaces"):
        replace(spec, regions=(spec.regions[0], replace(spec.regions[1], namespace=spec.regions[0].namespace)))
    group = spec.database_groups[0]
    with pytest.raises(ValueError, match="member origins"):
        replace(
            group,
            members=(group.members[0], replace(group.members[1], origin=group.members[0].origin)) + group.members[2:],
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema_version", True),
        ("stable_seconds", 600),
        ("deadline_seconds", 15),
        ("max_error_rate", float("nan")),
        ("max_error_rate", True),
        ("calibration_sha256", "unmeasured"),
    ],
)
def test_verification_limits_and_calibration_identity_are_validated(field, value):
    with pytest.raises(ValueError):
        replace(contract(), **{field: value})


def test_recovery_contract_cannot_silently_drop_history_or_business_outcomes():
    with pytest.raises(ValueError, match="match the incident family"):
        replace(contract(), recovery_rule=RecoveryRule.RETAINED_FLOOR_AND_FRESH_WRITES)
    with pytest.raises(ValueError, match="mandatory"):
        replace(contract(), required_behaviors=("journal-history", "current-records", "fresh-writes"))


def test_cleanup_cannot_delete_another_run_or_a_recreated_resource_with_the_same_name():
    owned, resource = inventory()
    with pytest.raises(ValueError, match="another run"):
        owned.with_resource(replace(resource, run_id=str(uuid4()), name="db-b-reader", uid=str(uuid4())))
    stopping = owned.transition(LifecyclePhase.STOPPING)
    replacement = replace(resource, uid=str(uuid4()))
    assert not stopping.owns(replacement)
    with pytest.raises(ValueError, match="exact owned"):
        stopping.mark_removed(replacement)
    with pytest.raises(ValueError, match="pending"):
        stopping.transition(LifecyclePhase.STOPPED)
    removed = stopping.mark_removed(resource)
    assert removed.cleanup_pending == ()
    stopped = removed.transition(LifecyclePhase.STOPPED)
    assert stopped.transition(LifecyclePhase.STOPPED) is stopped
    assert owned.cleanup_pending == (resource,)


def test_healthy_capture_cannot_be_rebased_and_failed_runs_cannot_reenter_execution():
    run = RunInventory(str(uuid4()), 1)
    with pytest.raises(ValueError, match="Illegal"):
        run.transition(LifecyclePhase.FAULTED)
    for phase in (
        LifecyclePhase.PROVISIONING,
        LifecyclePhase.HEALTHY,
        LifecyclePhase.BASELINE,
        LifecyclePhase.FAULTED,
        LifecyclePhase.HANDOFF,
        LifecyclePhase.RECOVERING,
        LifecyclePhase.VERIFYING,
    ):
        run = run.transition(phase)
    with pytest.raises(ValueError, match="Illegal"):
        run.transition(LifecyclePhase.BASELINE)
    failed = run.transition(LifecyclePhase.INVALID)
    with pytest.raises(ValueError, match="Illegal"):
        failed.transition(LifecyclePhase.HEALTHY)
    assert failed.transition(LifecyclePhase.STOPPING).phase == LifecyclePhase.STOPPING


def test_runtime_inventory_freezes_before_healthy_and_preserves_generation_identity():
    run, resource = inventory()
    healthy = run.transition(LifecyclePhase.PROVISIONING).transition(LifecyclePhase.HEALTHY)
    with pytest.raises(ValueError, match="freezes"):
        healthy.with_resource(replace(resource, name="new-pod", uid=str(uuid4())))
    assert healthy.run_id == run.run_id and healthy.generation == 1
    with pytest.raises(ValueError):
        replace(run, generation=True)


def test_dataset_manifest_is_measured_and_records_full_payload_provenance():
    dataset = manifest()
    assert dataset.operation_count == 200
    assert dataset.storage.temporary_restore_bytes == 5000
    assert dataset.recovery_sources[0].contains_full_payload
    digest_only = replace(dataset.recovery_sources[0], contains_full_payload=False)
    assert not digest_only.contains_full_payload
    with pytest.raises(ValueError):
        replace(dataset, storage={"sql_bytes": 1000})
    with pytest.raises(ValueError, match="empty queue"):
        replace(dataset, queue_count=0)
    with pytest.raises(ValueError, match="immutable tuple"):
        replace(dataset, entity_counts=list(dataset.entity_counts))


def test_evidence_freezes_only_closed_epoch_cuts_and_binds_their_hashes():
    run_id = str(uuid4())
    cut = HistoryCut(0, 200, 200, HASH, "b" * 64, 90)
    evidence = EvidenceSnapshot(run_id, 1, 100, manifest(), (cut,), (DigestEntry("receipts", HASH),))
    assert evidence.sha256 == replace(evidence).sha256
    assert evidence.sha256 != replace(evidence, closed_cuts=(replace(cut, receipts_sha256="c" * 64),)).sha256
    with pytest.raises(ValueError, match="not closed"):
        replace(evidence, captured_at_ns=89)
    with pytest.raises(ValueError, match="unique"):
        replace(evidence, closed_cuts=(cut, cut))
    with pytest.raises(ValueError, match="monotonic"):
        replace(evidence, closed_cuts=(cut, replace(cut, epoch=1, highest_receipt_sequence=100, accepted_count=50)))


@pytest.mark.parametrize(
    "factory",
    [
        lambda: HistoryCut(0, 2, 3, HASH, HASH, 1),
        lambda: DigestEntry("source", "A" * 64),
        lambda: StorageFootprint(1, 2, 3, 4, 5, True),
        lambda: SizeDistribution("object-bytes", 1, 10, 5, 20, 30),
        lambda: OwnedResource(str(uuid4()), "Pod", "", "api", "uid"),
    ],
)
def test_invalid_evidence_and_unscoped_resource_claims_fail_closed(factory):
    with pytest.raises(ValueError):
        factory()


def test_owned_helm_release_secret_accepts_real_kubernetes_subdomain_names():
    resource = OwnedResource(str(uuid4()), "Secret", "codehub-region-a", "sh.helm.release.v1.codehub.v1", str(uuid4()))
    assert resource.name == "sh.helm.release.v1.codehub.v1"


def test_owned_database_endpoint_slice_requires_exact_namespaced_identity():
    resource = OwnedResource(str(uuid4()), "EndpointSlice", "codehub-region-b", "mysql-g0-link-region-a", str(uuid4()))
    owned = RunInventory(resource.run_id, 1, resources=(resource,))
    assert owned.owns(resource)
    assert not owned.owns(replace(resource, uid=str(uuid4())))
    with pytest.raises(ValueError, match="namespace"):
        replace(resource, namespace="")


@pytest.mark.parametrize("name", ["../other", "a..b", "a/secret", "a.", "a" * 254])
def test_owned_resource_subdomain_validation_still_rejects_invalid_names(name):
    with pytest.raises(ValueError):
        OwnedResource(str(uuid4()), "Secret", "codehub-region-a", name, str(uuid4()))
