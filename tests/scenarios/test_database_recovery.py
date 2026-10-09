"""Safety and reproducibility contracts for private environment scaling."""

import json
from dataclasses import replace

import pytest

from sregym.conductor.scenarios.database_recovery import (
    TIERS,
    NoiseDecision,
    NoiseEvent,
    SeedStreams,
    noise_schedule,
    plan_noise,
)


def test_tiers_expand_actual_roles_tenants_and_state_within_lab_budget():
    previous = (0, 0, 0)
    for tier in TIERS.values():
        tier.admit(physical_cores=64, available_memory_gib=239, available_disk_gib=350)
        actual = (sum(count for _, _, count in tier.public_topology()), len(tier.noise_targets()), tier.records)
        assert all(new > old for old, new in zip(previous, actual, strict=True))
        previous = actual


@pytest.mark.parametrize(
    "resources",
    [
        {"physical_cores": 32, "available_memory_gib": 239},
        {"physical_cores": 64, "available_memory_gib": 128},
        {"physical_cores": 64, "available_memory_gib": 239, "reserve_memory_gib": 128},
    ],
)
def test_large_tier_cannot_consume_trusted_or_host_headroom(resources):
    with pytest.raises(ValueError, match="headroom"):
        TIERS["large"].admit(**({"available_disk_gib": 350} | resources))


@pytest.mark.parametrize("field,value", [("zones", 0), ("records", -1), ("cpu_limit", True), ("zones", 27)])
def test_invalid_tier_cannot_provision(field, value):
    with pytest.raises(ValueError):
        replace(TIERS["small"], **{field: value})


def test_noise_and_fault_streams_are_independent_and_public_topology_has_no_private_identity():
    streams = SeedStreams.derive(271)
    assert len({streams.topology, streams.data, streams.noise, streams.fault}) == 4
    noisy = noise_schedule(TIERS["medium"], streams.noise, horizon_seconds=900)
    different_fault = replace(streams, fault=streams.fault + 1)
    assert noisy == noise_schedule(TIERS["medium"], different_fault.noise, horizon_seconds=900)
    encoded = json.dumps(TIERS["medium"].public_topology()).lower()
    assert all(word not in encoded for word in ("sregym", "gitlab", "fault", "seed", "verifier", "medium"))


@pytest.mark.parametrize("tier", list(TIERS.values()))
def test_reproducible_noise_is_bounded_and_does_not_overlap_its_target(tier):
    events = noise_schedule(tier, 9, horizon_seconds=900)
    assert events and events == noise_schedule(tier, 9, horizon_seconds=900)
    assert events != noise_schedule(tier, 10, horizon_seconds=900)
    for event in events:
        assert 0 <= event.at_second < event.at_second + event.duration_seconds <= 900
        active = [e for e in events if e.at_second <= event.at_second < e.at_second + e.duration_seconds]
        assert len(active) <= 2
        assert len({e.target for e in active}) == len(active)


def test_disabled_noise_preserves_the_same_environment_and_has_no_actions():
    tier = TIERS["large"]
    before = tier.public_topology()
    assert noise_schedule(tier, 9, horizon_seconds=900, enabled=False) == ()
    assert tier.public_topology() == before


@pytest.mark.parametrize(
    "options",
    [
        {"max_concurrent": 0},
        {"horizon_seconds": 10},
        {"duration_seconds": 90},
        {"enabled": 1},
        {"interval_seconds": True},
    ],
)
def test_invalid_noise_configuration_fails_before_execution(options):
    with pytest.raises(ValueError):
        noise_schedule(TIERS["small"], 9, **({"horizon_seconds": 900} | options))


def test_restore_cannot_overcommit_storage():
    with pytest.raises(ValueError, match="disk headroom"):
        TIERS["large"].admit(physical_cores=64, available_memory_gib=239, available_disk_gib=100)


def test_regional_tiers_include_complete_placement_and_database_groups():
    assert [
        (tier.regions, tier.worker_nodes_per_region, tier.database_groups, tier.records) for tier in TIERS.values()
    ] == [(2, 3, 1, 20_000), (2, 3, 2, 200_000), (3, 3, 4, 2_000_000)]
    small = TIERS["small"]
    roles = {(region, role): count for region, role, count in small.public_topology()}
    for region in ("region-a", "region-b"):
        assert roles[region, "queue"] == 3
        assert roles[region, "gateway"] == 2
        assert roles[region, "database"] == 2
        assert roles[region, "repository"] == 1


def test_noise_disabled_comparison_keeps_all_requested_choices_and_ids():
    enabled = plan_noise(TIERS["large"], 913, horizon_seconds=900)
    disabled = plan_noise(TIERS["large"], 913, horizon_seconds=900, enabled=False)
    assert enabled.requested == disabled.requested
    assert [decision.event_id for decision in enabled.decisions] == [
        decision.event_id for decision in disabled.decisions
    ]
    assert not disabled.admitted
    assert all(item.rejection_reason == "noise-disabled" for item in disabled.rejected)
    assert enabled.rejected
    assert all(item.rejection_reason == "global-cap" for item in enabled.rejected)
    assert noise_schedule(TIERS["large"], 913, horizon_seconds=900) == enabled.admitted


def test_accounting_exposes_capped_noise_per_tenant_without_claiming_execution():
    plan = plan_noise(TIERS["large"], 913, horizon_seconds=900)
    counts = plan.counts_by_target()
    assert sum(requested for _, requested, _, _ in counts) == len(plan.decisions)
    assert sum(admitted for _, _, admitted, _ in counts) == len(plan.admitted)
    assert sum(rejected for _, _, _, rejected in counts) == len(plan.rejected)
    assert all(requested == admitted + rejected for _, requested, admitted, rejected in counts)
    assert not hasattr(plan, "executed")


@pytest.mark.parametrize(
    "admitted,reason",
    [(True, "global-cap"), (False, None), (False, ""), (False, "executed"), (1, None)],
)
def test_noise_admission_cannot_discard_or_mislabel_its_reason(admitted, reason):
    with pytest.raises(ValueError):
        NoiseDecision("scheduled", NoiseEvent(1, 15, "region-a/tenant-000", "traffic-burst"), admitted, reason)


@pytest.mark.parametrize("field,value", [("noise", True), ("fault", -1), ("data", 1.5)])
def test_seed_contract_rejects_invalid_direct_construction(field, value):
    with pytest.raises(ValueError):
        replace(SeedStreams.derive(4), **{field: value})
