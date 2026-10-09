"""Owner provenance assembly controls, without recapturing workload state."""

import hashlib
import json
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from uuid import UUID

import pytest

from sregym.conductor.oracles.codehub_state import effect_identity
from sregym.conductor.oracles.regional_database_recovery import (
    DeliveryObserverTarget,
    GitRefExpectation,
    HTTPServiceTarget,
    SQLTarget,
    WebhookDestination,
)
from sregym.conductor.scenarios.codehub_expectations import (
    DatabaseMemberRequirement,
    RegionalTargetInventory,
    RegionReplicaRequirement,
    WebhookSubscription,
    build_dataset_manifest,
    build_receipt_cuts,
    build_recovery_outcomes,
    compile_seed_git_inventory,
    merge_git_provenance,
)
from sregym.generators.workload.codehub import Operation, ReceiptLedger, canonical
from sregym.generators.workload.codehub_seed import TenantAccount


def uid(number):
    return str(UUID(int=number))


def accounts():
    return tuple(
        TenantAccount(
            tenant_id=uid(100 + index),
            project_id=uid(200 + index),
            region=f"region-{letter}",
            group=f"group-{letter}",
            owner_id=uid(index + 1),
            owner_token="customer-" + letter * 32,
            webhook_id=uid(900 + index),
            git_commit="b" * 40,
            git_files=(("app.py", "d" * 64),),
        )
        for index, letter in enumerate("ab")
    )


def operation(account, entity, revision, kind, payload, event):
    return Operation(
        uid(event),
        account.tenant_id,
        uid(entity),
        account.project_id,
        revision,
        kind,
        canonical(payload),
        account.owner_id,
    )


def provenance(account, commit, ref, source):
    return {
        "project_id": account.project_id,
        "refs": [{"ref": ref, "commit_sha": commit}],
        "files": [{"commit_sha": commit, "path": "app.py", "sha256": source}],
        "bundles": [{"commit_sha": commit, "sha256": hashlib.sha256(commit.encode()).hexdigest()}],
    }


def effects(op, account):
    entries = [("search", op.entity_id, op.request())]
    if op.kind == "repository.push":
        entries.append(("build", op.project_id, op.request() | {"commit_sha": op.request()["payload"]["commit_sha"]}))
    if op.kind in {"repository.push", "issue.create"}:
        entries.append(
            ("delivery", account.webhook_id, {"operation": op.request(), "url": "http://customer/deliveries"})
        )
    return [
        {
            "effect_id": effect_identity(op.event_id, kind, destination),
            "event_id": op.event_id,
            "effect_kind": kind,
            "destination": destination,
            "payload": payload,
        }
        for kind, destination, payload in entries
    ]


@pytest.fixture
def seeded(tmp_path):
    ledger = ReceiptLedger(tmp_path / "private" / "receipts.sqlite3")
    tenants = accounts()
    epoch = ledger.begin_epoch()
    for index, account in enumerate(tenants):
        for offset, (entity, revision, ref, commit, source) in enumerate(
            (
                (300 + index, 1, "refs/heads/main", "a" * 40, "c" * 64),
                (310 + index, 1, "refs/heads/feature", "b" * 40, "d" * 64),
                (300 + index, 2, "refs/heads/main", "b" * 40, "d" * 64),
            )
        ):
            op = operation(
                account,
                entity,
                revision,
                "repository.push",
                {"ref": ref, "commit_sha": commit},
                10 + index * 10 + offset,
            )
            ledger.request(op, epoch, effects=effects(op, account), provenance=provenance(account, commit, ref, source))
            ledger.acknowledge(op.event_id, "http://api", 1, 201)
        op = operation(
            account, 400 + index, 1, "issue.create", {"title": "Connection timeout", "state": "open"}, 13 + index * 10
        )
        ledger.request(op, epoch, effects=effects(op, account))
        ledger.acknowledge(op.event_id, "http://api", 1, 200)
    ledger.close_epoch(epoch)
    yield ledger, tenants, tuple((item.tenant_id, item.group) for item in tenants)
    ledger.close()


def inventory():
    databases = tuple(
        SQLTarget(
            f"group-{group}",
            f"region-{region}",
            f"platform-{region}",
            f"db-{group}-{region}-{role}",
            "codehub",
            "observer",
            "x" * 32,
        )
        for group in "ab"
        for region, role in (("a", "writer"), ("a", "reader"), ("b", "candidate"), ("b", "reader"))
    )
    targets = {
        role: tuple(
            HTTPServiceTarget(
                f"region-{letter}",
                f"platform-{letter}",
                role,
                resource_kind="deployment" if role == "api" else "statefulset",
                expected_replicas=2 if role == "api" else 1,
            )
            for letter in "ab"
        )
        for role in ("api", "search", "repository")
    }
    return RegionalTargetInventory(
        databases,
        targets["api"],
        targets["search"],
        targets["repository"],
        tuple(DatabaseMemberRequirement(item.group, item.region, item.namespace, item.service) for item in databases),
        tuple(RegionReplicaRequirement(f"region-{letter}", f"platform-{letter}", 2, 1, 1) for letter in "ab"),
    )


def test_cuts_match_owner_history_and_effect_hashes_without_large_payload_snapshot(seeded, monkeypatch):
    ledger, _tenants, routing = seeded
    expected = ledger.expected_effects(dict(routing))
    monkeypatch.setattr(ledger, "expected_effects", lambda _routing: pytest.fail("Bulk effect copying is unnecessary"))
    cuts, effect_cuts = build_receipt_cuts(ledger, routing)
    assert [cut.group for cut in cuts] == ["group-a", "group-b"]
    assert [cut.operations for cut in cuts] == [4, 4]
    assert all(cut.closed_epochs == (0,) for cut in cuts)
    for cut in effect_cuts:
        checksum = hashlib.sha256()
        for row in expected[cut.group]:
            checksum.update(canonical(row).encode() + b"\n")
        assert (cut.count, cut.sha256) == (len(expected[cut.group]), checksum.hexdigest())
        assert not hasattr(cut, "path") and not hasattr(cut, "payload")


@pytest.mark.parametrize("routing", [(), ((uid(100), "group-a"), (uid(100), "group-b")), {uid(100): "group-a"}])
def test_missing_mutable_or_duplicate_routing_is_rejected(seeded, routing):
    with pytest.raises(ValueError):
        build_receipt_cuts(seeded[0], routing)


def test_declared_tenant_without_closed_receipts_is_rejected(seeded):
    ledger, _tenants, routing = seeded
    with pytest.raises(ValueError, match="omit declared tenants"):
        build_receipt_cuts(ledger, routing + ((uid(999), "group-a"),))


def test_missing_per_operation_search_expectation_is_a_setup_failure(seeded):
    ledger, _tenants, routing = seeded
    row = ledger._db.execute("SELECT effects FROM expectations WHERE event_id=?", (uid(13),)).fetchone()[0]
    ledger._db.execute(
        "UPDATE expectations SET effects=? WHERE event_id=?",
        (canonical([entry for entry in json.loads(row) if entry["effect_kind"] != "search"]), uid(13)),
    )
    with pytest.raises(ValueError, match="search/build expectations"):
        build_receipt_cuts(ledger, routing)


def test_open_live_traffic_does_not_replace_the_closed_cut(seeded):
    ledger, tenants, routing = seeded
    original = build_receipt_cuts(ledger, routing)
    epoch = ledger.begin_epoch()
    op = operation(tenants[0], 700, 1, "issue.create", {"title": "Retry jitter", "state": "open"}, 70)
    ledger.request(op, epoch, effects=effects(op, tenants[0]))
    assert build_receipt_cuts(ledger, routing) == original


def test_dataset_manifest_keeps_original_owner_cut_after_later_traffic_and_refuses_wrong_digest(seeded):
    ledger, tenants, routing = seeded
    cuts, _effects = build_receipt_cuts(ledger, routing)
    physical = {
        "storage": {
            name: index
            for index, name in enumerate(
                (
                    "sql_bytes",
                    "index_bytes",
                    "git_bytes",
                    "artifact_bytes",
                    "retained_log_bytes",
                    "temporary_restore_bytes",
                )
            )
        }
    }
    groups = tuple(
        SimpleNamespace(name=tenant.group, members=(SimpleNamespace(name="db-" + tenant.group, region=tenant.region),))
        for tenant in tenants
    )
    manifest = build_dataset_manifest(
        ledger, tenants, cuts, groups, physical, queue_count=3, oldest_queue_age_seconds=10
    )
    assert manifest.operation_count == 8 and manifest.queue_count == 3
    assert manifest.schema_version == 2 and sum(item.count for item in manifest.operation_mix) == 8
    assert {item.kind for item in manifest.operation_mix} == {"repository.push", "issue.create"}
    assert all(item.project in {tenant.project_id for tenant in tenants} for item in manifest.operation_mix)
    assert all(item.group == dict(routing)[item.tenant] for item in manifest.operation_mix)
    with pytest.raises(ValueError, match="complete accepted operation count"):
        replace(manifest, operation_mix=manifest.operation_mix[:-1])
    with pytest.raises(ValueError, match="unique"):
        replace(manifest, operation_mix=manifest.operation_mix + manifest.operation_mix[:1])
    assert sum(item.count for item in manifest.entity_counts if item.entity == "repository") == 4
    assert sum(item.count for item in manifest.entity_counts if item.entity == "issue") == 2
    assert manifest.distributions[0].samples == 8 and manifest.storage.retained_log_bytes == 4
    assert {item.sha256 for item in manifest.recovery_sources} == {cut.journal_sha256 for cut in cuts}
    epoch = ledger.begin_epoch()
    op = operation(tenants[0], 999, 1, "issue.create", {"title": "Later accepted traffic", "state": "open"}, 999)
    ledger.request(op, epoch, effects=effects(op, tenants[0]))
    ledger.acknowledge(op.event_id, "http://api", 1, 201)
    ledger.close_epoch(epoch)
    assert (
        build_dataset_manifest(ledger, tenants, cuts, groups, physical, queue_count=3, oldest_queue_age_seconds=10)
        == manifest
    )
    wrong = (replace(cuts[0], journal_sha256="0" * 64), *cuts[1:])
    with pytest.raises(ValueError, match="original protected receipt watermark"):
        build_dataset_manifest(ledger, tenants, wrong, groups, physical, queue_count=3, oldest_queue_age_seconds=10)


def test_compiler_uses_revisions_not_random_event_order_and_keeps_historical_source(seeded):
    ledger, tenants, _routing = seeded
    entries = tuple(reversed(ledger.git_provenance_entries()))
    seed = compile_seed_git_inventory(tenants, entries)
    assert len(seed) == 2
    for project in seed:
        assert {ref.ref: ref.commit_sha for ref in project.refs} == {
            "refs/heads/main": "b" * 40,
            "refs/heads/feature": "b" * 40,
        }
        assert {item.commit_sha for item in project.files} == {"a" * 40, "b" * 40}
        assert {item.commit_sha for item in project.bundles} == {"a" * 40, "b" * 40}
    assert merge_git_provenance(seed, entries) == seed


def test_fresh_commit_additions_preserve_seed_sources_and_dont_mutate_input(seeded):
    ledger, tenants, _routing = seeded
    seed = compile_seed_git_inventory(tenants, ledger.git_provenance_entries())
    original = seed
    op = operation(tenants[0], 800, 1, "repository.push", {"ref": "refs/heads/change-2027", "commit_sha": "e" * 40}, 80)
    entry = {
        "operation": op.observed_row(),
        "provenance": provenance(tenants[0], "e" * 40, "refs/heads/change-2027", "f" * 64),
    }
    entries = (entry,)
    before = deepcopy(entries)
    merged = merge_git_provenance(seed, entries)
    assert seed is original and entries == before
    updated = next(item for item in merged if item.project_id == tenants[0].project_id)
    assert set(seed[0].files) <= set(updated.files) and set(seed[0].bundles) <= set(updated.bundles)
    assert GitRefExpectation("refs/heads/change-2027", "e" * 40) in updated.refs


@pytest.mark.parametrize("field", ["files", "bundles"])
def test_same_commit_cannot_rewrite_frozen_content_hashes(seeded, field):
    ledger, tenants, _routing = seeded
    entries = ledger.git_provenance_entries()
    seed = compile_seed_git_inventory(tenants, entries)
    changed = deepcopy(entries)
    changed[0]["provenance"][field][0]["sha256"] = "f" * 64
    with pytest.raises(ValueError, match="different|replace"):
        merge_git_provenance(seed, changed)


def test_new_receipt_cannot_hijack_an_existing_ref_without_its_history(seeded):
    ledger, tenants, _routing = seeded
    seed = compile_seed_git_inventory(tenants, ledger.git_provenance_entries())
    op = operation(tenants[0], 999, 1, "repository.push", {"ref": "refs/heads/main", "commit_sha": "e" * 40}, 80)
    with pytest.raises(ValueError, match="accepted history"):
        merge_git_provenance(
            seed,
            (
                {
                    "operation": op.observed_row(),
                    "provenance": provenance(tenants[0], "e" * 40, "refs/heads/main", "f" * 64),
                },
            ),
        )


def test_seed_metadata_must_match_independent_git_inventory(seeded):
    ledger, tenants, _routing = seeded
    with pytest.raises(ValueError, match="Seed main reference"):
        compile_seed_git_inventory(
            (replace(tenants[0], git_commit="e" * 40), tenants[1]), ledger.git_provenance_entries()
        )


@pytest.mark.parametrize(
    "change",
    [
        lambda value: replace(value, databases=value.databases[:-1]),
        lambda value: replace(value, api_targets=value.api_targets[:1]),
        lambda value: replace(value, search_targets=value.search_targets * 2),
        lambda value: replace(value, regions=(replace(value.regions[0], api_replicas=True), value.regions[1])),
    ],
)
def test_missing_duplicate_or_malformed_replica_inventory_is_rejected(change):
    with pytest.raises(ValueError):
        change(inventory())


def test_complete_outcomes_use_customer_metadata_private_pipe_and_all_replica_targets(seeded):
    ledger, tenants, routing = seeded
    seed = compile_seed_git_inventory(tenants, ledger.git_provenance_entries())
    hooks = tuple(
        WebhookSubscription(
            account.tenant_id,
            WebhookDestination(
                account.webhook_id, "http://customer/deliveries", ("issue.create", "issue.update", "repository.push")
            ),
        )
        for account in tenants
    )
    value = build_recovery_outcomes(
        tenants,
        routing,
        seed_projects=seed,
        git_entries=ledger.git_provenance_entries(),
        inventory=inventory(),
        observer=DeliveryObserverTarget("", "", transport="private_pipe"),
        service_token="service-" + "s" * 32,
        webhooks=hooks,
    )
    assert {item.tenant_id for item in value.fresh_challenges} == {item.tenant_id for item in tenants}
    assert value.observer.transport == "private_pipe"
    assert sum(item.expected_replicas for item in value.api_targets) == 4
    assert "customer-" not in repr(value) and "service-" not in repr(value)
    with pytest.raises(ValueError, match="omits a declared tenant webhook"):
        build_recovery_outcomes(
            tenants,
            routing,
            seed_projects=seed,
            git_entries=(),
            inventory=inventory(),
            observer=value.observer,
            service_token="s" * 32,
            webhooks=hooks[:1],
        )


@pytest.mark.parametrize("failure", ["none", "cancel", "pages"])
def test_effect_sorting_uses_owner_scratch_and_bounded_cleanup(seeded, monkeypatch, failure):
    from sregym.conductor.scenarios import codehub_expectations as module

    ledger, _tenants, routing = seeded
    observed = []
    real = module.sqlite3.connect

    def connect(path, *args, **kwargs):
        observed.append(path)
        assert Path(path).parent.parent == ledger._path.parent
        return real(path, *args, **kwargs)

    from pathlib import Path

    monkeypatch.setattr(module.sqlite3, "connect", connect)
    ledger._snapshot_guard = lambda: int(failure == "cancel")
    if failure == "pages":
        ledger._byte_budget = 32768
    if failure == "none":
        assert build_receipt_cuts(ledger, routing)[1]
        assert observed
    else:
        with pytest.raises((RuntimeError, module.sqlite3.OperationalError)):
            build_receipt_cuts(ledger, routing)
    assert not list(ledger._path.parent.glob("private-effects-*"))
    assert ledger._db.execute("SELECT COUNT(*) FROM requests").fetchone()[0] > 0
