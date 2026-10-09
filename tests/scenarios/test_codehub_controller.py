"""Ownership, immutable epoch snapshots and normal-source history merge checks."""

import hashlib
import json
import sys
import threading
import time
from dataclasses import replace
from types import ModuleType, SimpleNamespace
from uuid import uuid4

import pytest

from sregym.conductor.scenarios import codehub_controller as controller_module
from sregym.conductor.scenarios.codehub_contracts import LifecyclePhase
from sregym.conductor.scenarios.codehub_controller import RecoveryController
from sregym.conductor.scenarios.codehub_reference_repair import (
    DatabaseReferenceRepair,
    RecoveredHistory,
    rewrite_worker_group_route,
    worker_group_routes,
)
from sregym.conductor.scenarios.database_recovery import TIERS
from sregym.generators.workload.codehub import Operation, ReceiptLedger, canonical
from sregym.generators.workload.codehub_seed import RegionEndpoints, SeedResult, TenantAccount


def controller(tmp_path):
    account = TenantAccount(
        str(uuid4()), str(uuid4()), "region-a", "group-0", str(uuid4()), "token" * 8, webhook_id=str(uuid4())
    )
    app = SimpleNamespace(regions=(SimpleNamespace(name="region-a"),), database_groups=())
    ledger = ReceiptLedger(tmp_path / "receipts.sqlite")
    instance = RecoveryController(
        app,
        tmp_path,
        {"region-a": RegionEndpoints("http://api", "http://git", "http://topology")},
        bootstrap_token="b" * 40,
        service_token="s" * 40,
        delivery_url="http://receiver/deliveries",
        lease_path=tmp_path / "campaign.lock",
        ledger=ledger,
        epoch_seconds=1,
    )
    instance.seed_result = SeedResult((account,), 0, ())

    class Client:
        pending = False

        def submit(self, operation, *, epoch, effects=(), provenance=None, **_kwargs):
            ledger.request(operation, epoch, effects=effects, provenance=provenance)
            if self.pending:
                return False
            ledger.acknowledge(operation.event_id, "http://api", 1, 201)
            return True

        def close(self):
            pass

    client = Client()
    instance._clients[account.tenant_id] = client
    return instance, ledger, client


@pytest.mark.parametrize("tier_name,expected", [("small", 8), ("medium", 16), ("large", 16)])
def test_seed_parallelism_uses_actual_api_capacity_with_explicit_global_cap(tmp_path, tier_name, expected):
    tier = TIERS[tier_name]
    regions = tuple(SimpleNamespace(name=f"region-{chr(97 + index)}") for index in range(tier.regions))
    application = SimpleNamespace(regions=regions, database_groups=(), tier=tier)
    instance = RecoveryController(
        application,
        tmp_path,
        {region.name: RegionEndpoints("http://api", "http://git", "http://topology") for region in regions},
        bootstrap_token="b" * 40,
        service_token="s" * 40,
        delivery_url="http://receiver/deliveries",
        lease_path=tmp_path / "unused.lock",
    )
    assert instance._seed_fill_workers() == expected
    instance.app.tier = replace(tier, tenants_per_zone=1)
    assert instance._seed_fill_workers() == len(regions)
    instance.stop()


def test_owned_prepare_passes_bounded_workers_to_actual_seeder_before_seed(tmp_path, monkeypatch):
    observed, ownership = {}, []
    application = SimpleNamespace(
        regions=(SimpleNamespace(name="region-a"), SimpleNamespace(name="region-b")),
        database_groups=(SimpleNamespace(members=(SimpleNamespace(role="writer"),)),),
        tier=TIERS["small"],
        inventory=SimpleNamespace(phase=LifecyclePhase.HEALTHY),
    )

    class Seeder:
        def __init__(self, **kwargs):
            observed.update(kwargs)

        def seed(self, tier):
            assert tier is application.tier
            raise RuntimeError("Stop before any live seed operation")

    monkeypatch.setattr(controller_module, "CustomerSeeder", Seeder)
    instance = RecoveryController(
        application,
        tmp_path,
        {region.name: RegionEndpoints("http://api", "http://git", "http://topology") for region in application.regions},
        bootstrap_token="b" * 40,
        service_token="s" * 40,
        delivery_url="http://receiver/deliveries",
        lease_path=tmp_path / "unused.lock",
        lease=SimpleNamespace(acquire=lambda: ownership.append("acquired"), close=lambda: ownership.append("closed")),
    )
    with pytest.raises(RuntimeError, match="before any live seed"):
        instance.prepare()
    assert observed["fill_workers"] == 8
    assert ownership == ["acquired", "closed"] and instance._stopped


def test_interleaved_verifier_epochs_cannot_collide_or_pause_traffic(tmp_path):
    instance, ledger, _client = controller(tmp_path)
    assert instance.traffic_step()["acknowledged"] == 2
    verifier_epoch = ledger.begin_epoch()
    instance._epoch_started -= 2
    assert instance.traffic_step()["acknowledged"] == 2
    snapshot = instance.verification_inputs()
    assert instance._epoch > verifier_epoch
    assert snapshot.receipt_cut.operations == 2
    assert len(snapshot.expected_effects[0][1]) == 4
    assert not instance.cancel.is_set()
    assert instance.traffic_step()["acknowledged"] == 2
    assert snapshot.receipt_cut.operations == 2
    instance.stop()


def test_timeout_outcome_resubmits_identical_private_metadata_and_blocks_closure(tmp_path):
    instance, ledger, client = controller(tmp_path)
    client.pending = True
    assert instance.traffic_step()["acknowledged"] == 0
    original = ledger.pending_requests()[0]
    instance._epoch_started -= 2
    instance.traffic_step()
    assert instance.verification_inputs().receipt_cut.operations == 0
    assert ledger.pending_requests()[0] == original
    client.pending = False
    assert instance.resolve_pending()
    instance._epoch_started -= 2
    instance.traffic_step()
    assert instance.verification_inputs().receipt_cut.operations >= 2
    instance.stop()


def row(operation, accepted):
    body = operation.observed_row()
    return {**body, "accepted_at": accepted, "payload_sha256": hashlib.sha256(canonical(body).encode()).hexdigest()}


def test_normal_source_merge_retains_both_suffixes_and_deduplicates_exact_history(tmp_path):
    tenant, entity, actor, project = (str(uuid4()) for _ in range(4))
    history = RecoveredHistory(tmp_path / "recovered.sqlite")
    operations = [
        Operation(
            str(uuid4()),
            tenant,
            entity,
            project,
            revision,
            "issue.create" if revision == 1 else "issue.update",
            canonical({"title": "Retry timing", "body": f"Review {revision}"}),
            actor,
        )
        for revision in (1, 2, 3)
    ]
    for index in (0, 1, 0, 2):
        history.add_operation(row(operations[index], f"2026-10-09 01:00:0{index}.000001"))
    path = history.write_stream(tmp_path / "history.jsonl")
    restored = [json.loads(line)["operation"] for line in path.read_text().splitlines()]
    assert [item["client_revision"] for item in restored] == [1, 2, 3]
    assert {item["event_id"] for item in restored} == {operation.event_id for operation in operations}
    history.close()


def test_conflicting_source_identity_or_revision_fails_closed(tmp_path):
    operation = Operation(
        str(uuid4()),
        str(uuid4()),
        str(uuid4()),
        str(uuid4()),
        1,
        "issue.create",
        canonical({"title": "Retries"}),
        str(uuid4()),
    )
    history = RecoveredHistory(tmp_path / "recovered.sqlite")
    history.add_operation(row(operation, "2026-10-09 01:00:00.000001"))
    with pytest.raises(RuntimeError, match="event identity"):
        history.add_operation(row(replace(operation, actor_id=str(uuid4())), "2026-10-09 01:00:00.000001"))
    with pytest.raises(RuntimeError, match="record revision"):
        history.add_operation(row(replace(operation, event_id=str(uuid4())), "2026-10-09 01:00:00.000001"))
    invalid = row(replace(operation, event_id=str(uuid4()), client_revision=2), "2026-10-09 01:00:01.000001")
    invalid["payload_sha256"] = "0" * 64
    with pytest.raises(RuntimeError, match="invalid operation digest"):
        history.add_operation(invalid)
    history.close()


def test_replaced_namespace_blocks_owned_actions_before_mutation(tmp_path):
    instance, _ledger, _client = controller(tmp_path)
    core = SimpleNamespace(
        read_namespace=lambda *_args, **_kwargs: SimpleNamespace(metadata=SimpleNamespace(uid="replacement"))
    )
    instance.app._client = lambda: SimpleNamespace(core_v1_api=core)
    instance.app.inventory = SimpleNamespace(
        resources=(SimpleNamespace(kind="Namespace", name="codehub-a", uid="captured"),)
    )
    with pytest.raises(RuntimeError, match="ownership changed"):
        instance._assert_namespace("codehub-a")
    instance.stop()


def test_replaced_worker_deployment_blocks_route_patch(tmp_path, monkeypatch):
    instance, _ledger, _client = controller(tmp_path)
    regions = (
        SimpleNamespace(name="region-a", namespace="codehub-a"),
        SimpleNamespace(name="region-b", namespace="codehub-b"),
    )
    writer = SimpleNamespace(role="writer", region="region-a", origin="mysql://writer.codehub-a.svc:3306")
    candidate = SimpleNamespace(role="candidate", region="region-b")
    instance.app.regions = regions
    instance.app.database_groups = (SimpleNamespace(name="group-0", members=(writer, candidate)),)
    instance.app.inventory = SimpleNamespace(
        resources=(
            SimpleNamespace(kind="Deployment", name="worker", namespace="codehub-b", uid="captured"),
            SimpleNamespace(kind="Namespace", name="codehub-b", uid="namespace"),
        )
    )
    instance._assert_namespace = lambda _namespace: None
    calls = []
    api = SimpleNamespace(
        read_namespaced_deployment=lambda *_args, **_kwargs: SimpleNamespace(
            metadata=SimpleNamespace(uid="replacement")
        ),
        patch_namespaced_deployment=lambda *_args, **_kwargs: calls.append("patch"),
    )
    module = ModuleType("kubernetes.client")
    module.AppsV1Api = lambda _client: api
    monkeypatch.setitem(sys.modules, "kubernetes.client", module)
    instance.app._client = lambda: SimpleNamespace(
        core_v1_api=SimpleNamespace(
            api_client=None,
            read_namespace=lambda *_args, **_kwargs: SimpleNamespace(metadata=SimpleNamespace(uid="namespace")),
        ),
        exec_command_checked=lambda *_args, **_kwargs: calls.append("exec"),
    )
    with pytest.raises(RuntimeError, match="not captured as owned"):
        instance._prepare_worker_route(writer)
    assert calls == []
    instance.stop()


def test_failed_real_noise_invalidates_verification(tmp_path):
    instance, _ledger, _client = controller(tmp_path)
    instance.noise = SimpleNamespace(
        receipts=(SimpleNamespace(admitted=True, status="failed"),), stop=lambda **_kwargs: None
    )
    with pytest.raises(RuntimeError, match="required real-noise"):
        instance.verification_inputs()
    instance.stop()


def test_atomic_snapshot_holds_both_owner_locks_without_stop(tmp_path):
    instance, ledger, _client = controller(tmp_path)
    instance.traffic_step()
    instance._epoch_started -= 2
    instance.traffic_step()

    def builder(inputs, source):
        assert instance._lock._is_owned() and source._lock._is_owned()
        assert inputs.tenants == instance.seed_result.tenants
        assert not hasattr(inputs, "receipt_cut") and not hasattr(inputs, "expected_effects")
        return inputs

    snapshot = instance.verification_snapshot(builder)
    assert (
        ledger.cut().operations == 2 and snapshot.tenants == instance.seed_result.tenants and ledger is instance.ledger
    )
    assert not instance.cancel.is_set()
    instance.stop()


def test_compact_snapshot_does_not_materialize_bulk_receipts_or_effects(tmp_path, monkeypatch):
    instance, ledger, _client = controller(tmp_path)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("Large receipt materialization is forbidden in the compact seam")

    for name in ("cut", "partition_cuts", "expected_effects", "git_provenance_entries"):
        monkeypatch.setattr(ledger, name, forbidden)
    result = instance.verification_snapshot(lambda inputs, _ledger: inputs)
    assert result.tenants == instance.seed_result.tenants
    instance.stop()


def test_worker_route_update_preserves_unaffected_groups_and_read_configuration():
    routes = {
        "*": {
            "group": "group-0",
            "writer_host": "mysql-g0-writer-route",
            "reader_host": "mysql-g0-reader",
            "port": 3306,
        },
        "tenant-a": {
            "group": "group-0",
            "writer_host": "mysql-g0-writer-route",
            "reader_host": "mysql-g0-reader",
            "port": 3306,
        },
        "tenant-b": {
            "group": "group-1",
            "writer_host": "mysql-g1-writer-route",
            "reader_host": "mysql-g1-reader",
            "port": 3306,
            "read_port": 3307,
        },
    }
    pinned = worker_group_routes(routes, "group-0", "172.17.0.1", 25001)
    assert pinned["tenant-b"] == routes["tenant-b"]
    assert routes["tenant-a"]["writer_host"] == "mysql-g0-writer-route"
    assert pinned["tenant-a"]["reader_host"] == "mysql-g0-reader"
    assert pinned["tenant-a"]["writer_host"] == "172.17.0.1" and pinned["*"]["port"] == 25001
    restored = worker_group_routes(pinned, "group-0", "mysql-g0-writer-route", 3306)
    assert restored == routes


def test_reference_repair_only_mutates_selected_group(tmp_path, monkeypatch):
    groups = tuple(
        SimpleNamespace(
            name=f"group-{index}",
            members=tuple(
                SimpleNamespace(
                    name=f"g{index}-{role}",
                    role=role,
                    region="region-a" if role == "writer" else "region-b",
                    origin=f"mysql://mysql-g{index}-{role}.svc:3306",
                )
                for role in ("writer", "candidate", "reader")
            ),
        )
        for index in (0, 1)
    )
    queries, routes, copies = [], [], []

    def mysql(member, query):
        queries.append((member.name, query))
        if "replication_connection_configuration" in query:
            return '{"channels":0}'
        if "@@GLOBAL.gtid_executed" in query:
            return '{"gtid":"aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa:1-9"}'
        if "WAIT_FOR_EXECUTED_GTID_SET" in query:
            return '{"wait":0}'
        return ""

    app = SimpleNamespace(
        database_groups=groups,
        regions=tuple(SimpleNamespace(name=f"region-{letter}", namespace=f"codehub-{letter}") for letter in "ab"),
        mysql_command=mysql,
        _credentials={"replication-password": "ordinary-password"},
        set_writer_route=lambda region, member: routes.append((region, member.name)),
        normal_connection_endpoint=lambda source_region, member: (
            "mysql-link-g0-to-region-b.codehub-a.svc.cluster.local"
            if source_region != member.region
            else "mysql-g0-candidate.codehub-b.svc.cluster.local",
            3306,
        ),
    )
    repair = DatabaseReferenceRepair(app, tmp_path)
    repair._assert_owner = lambda _member: None
    operation = Operation(
        str(uuid4()),
        str(uuid4()),
        str(uuid4()),
        str(uuid4()),
        1,
        "issue.create",
        canonical({"title": "Retries"}),
        str(uuid4()),
    )
    repair._export = lambda _member, history: history.add_operation(row(operation, "2026-10-09 01:00:00.000001"))
    repair._copy_database = lambda source, destination, **_kwargs: copies.append((source.name, destination.name))
    repair._restore_worker_route = lambda namespace, group: routes.append((namespace, group))
    monkeypatch.setattr(
        sys.modules["sregym.conductor.scenarios.codehub_reference_repair"].subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0),
    )
    result = repair.run()
    assert result["group"] == "group-0" and result["recovered_operations"] == 1
    assert all(name.startswith("g0-") for name, _query in queries)
    assert all(source == "g0-candidate" and destination.startswith("g0-") for source, destination in copies)
    replication = {name: query for name, query in queries if "CHANGE REPLICATION SOURCE" in query}
    assert "SOURCE_HOST='mysql-link-g0-to-region-b.codehub-a.svc.cluster.local'" in replication["g0-writer"]
    assert "SOURCE_HOST='mysql-g0-candidate.codehub-b.svc.cluster.local'" in replication["g0-reader"]
    assert routes == [
        ("region-a", "g0-candidate"),
        ("codehub-a", "group-0"),
        ("region-b", "g0-candidate"),
        ("codehub-b", "group-0"),
    ]


@pytest.mark.parametrize("endpoint", [("127.0.0.1", 18474), ("ordinary; DROP DATABASE codehub", 3306), ("mysql", True)])
def test_reference_recovery_rejects_invalid_normal_endpoint_before_database_changes(tmp_path, endpoint):
    writer = SimpleNamespace(name="writer", role="writer", region="region-a")
    candidate = SimpleNamespace(name="candidate", role="candidate", region="region-b")
    app = SimpleNamespace(
        database_groups=(SimpleNamespace(name="group-0", members=(writer, candidate)),),
        normal_connection_endpoint=lambda _region, _member: endpoint,
        mysql_command=lambda *_args: pytest.fail("Endpoint validation must precede any database fencing"),
    )
    with pytest.raises(ValueError, match="ordinary owned SQL service"):
        DatabaseReferenceRepair(app, tmp_path / "uncreated").run()
    assert not (tmp_path / "uncreated").exists()


@pytest.mark.parametrize("actual_uid", ["captured-config", "replacement-config"])
def test_worker_map_mutation_is_uid_qualified_and_does_not_restart_other_groups(actual_uid, monkeypatch):
    updates, commands = [], []
    original = {
        "*": {"group": "group-0", "writer_host": "mysql-g0-writer-route", "port": 3306},
        "other": {"group": "group-1", "writer_host": "mysql-g1-writer-route", "port": 3306},
    }
    core = SimpleNamespace(
        read_namespace=lambda *_args, **_kwargs: SimpleNamespace(metadata=SimpleNamespace(uid="namespace")),
        read_namespaced_config_map=lambda *_args, **_kwargs: SimpleNamespace(
            metadata=SimpleNamespace(uid=actual_uid, resource_version="17"),
            data={"database-routes.json": canonical(original)},
        ),
        patch_namespaced_config_map=lambda *_args, **kwargs: updates.append(kwargs["body"]),
    )
    client = SimpleNamespace(
        core_v1_api=core, exec_command_checked=lambda command, **_kwargs: commands.append(command) or '{"applied":true}'
    )
    app = SimpleNamespace(
        regions=(SimpleNamespace(name="region-b", namespace="codehub-b"),),
        _client=lambda: client,
        inventory=SimpleNamespace(
            resources=(
                SimpleNamespace(kind="Namespace", name="codehub-b", uid="namespace"),
                SimpleNamespace(
                    kind="ConfigMap", name="worker-database-routes", namespace="codehub-b", uid="captured-config"
                ),
            )
        ),
    )
    module = sys.modules["sregym.conductor.scenarios.codehub_reference_repair"]
    monkeypatch.setattr(
        module, "owned_worker_pods", lambda *_args: (SimpleNamespace(metadata=SimpleNamespace(name="worker-owned")),)
    )
    if actual_uid != "captured-config":
        with pytest.raises(RuntimeError, match="ConfigMap ownership changed"):
            rewrite_worker_group_route(app, "region-b", "group-0", "172.17.0.1", 25001)
        assert updates == commands == []
    else:
        rewrite_worker_group_route(app, "region-b", "group-0", "172.17.0.1", 25001)
        assert len(updates) == 1 and updates[0]["metadata"] == {"resourceVersion": "17", "uid": "captured-config"}
        actual = json.loads(updates[0]["data"]["database-routes.json"])
        assert actual["other"] == original["other"]
        assert actual["*"]["writer_host"] == "172.17.0.1"
        assert len(commands) == 1 and "exec worker-owned" in commands[0]
        assert not any("rollout" in command or "set env" in command for command in commands)


def add_healthy_group(instance, ledger):
    original = instance.seed_result.tenants[0]
    account = replace(
        original,
        tenant_id=str(uuid4()),
        project_id=str(uuid4()),
        owner_id=str(uuid4()),
        group="group-1",
        webhook_id=str(uuid4()),
    )

    class Client:
        def submit(self, operation, *, epoch, effects=(), **_kwargs):
            assert not instance._lock._is_owned()
            ledger.request(operation, epoch, effects=effects)
            ledger.acknowledge(operation.event_id, "http://healthy-api", 1, 201)
            return True

        def close(self):
            pass

    instance.seed_result = replace(instance.seed_result, tenants=(*instance.seed_result.tenants, account))
    instance._clients[account.tenant_id] = Client()
    return account


def test_unaffected_group_progresses_during_selected_group_timeout_and_epoch_rotation(tmp_path):
    instance, ledger, _client = controller(tmp_path)
    selected = instance.seed_result.tenants[0]
    healthy = add_healthy_group(instance, ledger)
    blocked, release = threading.Event(), threading.Event()
    errors = []

    class DelayedClient:
        delayed = False

        def submit(self, operation, *, epoch, effects=(), **_kwargs):
            assert not instance._lock._is_owned()
            ledger.request(operation, epoch, effects=effects)
            ledger.acknowledge(operation.event_id, "http://selected-api", 1, 201)
            if not self.delayed:
                self.delayed = True
                blocked.set()
                assert release.wait(3)
            return True

        def close(self):
            pass

    instance._clients[selected.tenant_id] = DelayedClient()

    def selected_step():
        try:
            instance.traffic_step(tenant=selected)
        except Exception as exc:
            errors.append(exc)

    thread = threading.Thread(target=selected_step)
    thread.start()
    assert blocked.wait(1)
    instance._epoch_started -= 2
    start = time.monotonic()
    assert instance.traffic_step(tenant=healthy)["acknowledged"] == 2
    assert time.monotonic() - start < 1 and thread.is_alive()
    assert ledger.cut().operations == 0  # The first selected response cannot close a live mutation journey.
    release.set()
    thread.join(2)
    assert not thread.is_alive() and errors == []
    assert ledger.cut().operations == 2
    instance.stop()


def test_selected_pending_capacity_does_not_block_unaffected_group(tmp_path):
    instance, ledger, selected_client = controller(tmp_path)
    selected, healthy = instance.seed_result.tenants[0], add_healthy_group(instance, ledger)
    selected_client.pending = True
    for _index in range(32):
        instance.traffic_step(tenant=selected)
    assert len(ledger.pending_requests()) == 32
    capped = instance.traffic_step(tenant=selected)
    assert capped["requests"] == 1 and capped["pending_capacity_group"] == 32
    assert instance.traffic_step(tenant=healthy)["acknowledged"] == 2
    assert len(ledger.pending_requests()) == 32
    instance.stop()


def test_normal_route_projection_is_proved_before_worker_pin(tmp_path):
    instance, _ledger, _client = controller(tmp_path)
    events = []
    instance._route_tenant_writer = lambda tenant, group: events.append(("route", tenant, group))
    instance._configure_tenant_route("normal-tenant", "group-1")
    instance.app.await_database_routes = lambda routes, **_kwargs: events.append(("mounted", routes))
    instance._prepare_worker_route = lambda _writer: events.append(("pin",))
    instance._routes_ready(SimpleNamespace())
    assert events == [("route", "normal-tenant", "group-1"), ("mounted", {"normal-tenant": "group-1"}), ("pin",)]
    instance.stop()
