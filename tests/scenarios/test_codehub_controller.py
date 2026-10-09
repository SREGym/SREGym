"""Ownership, immutable epoch snapshots and normal-source history merge checks."""

import hashlib
import json
import os
import sqlite3
import subprocess
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


@pytest.mark.parametrize("tier_name,expected", [("small", 8), ("medium", 16), ("large", 48)])
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


def test_recovery_retains_candidate_history_and_replays_only_missing_suffix(tmp_path):
    tenant, entity, project, actor = (str(uuid4()) for _ in range(4))
    operations = [
        Operation(
            str(uuid4()),
            tenant,
            entity,
            project,
            revision,
            "issue.create" if revision == 1 else "issue.update",
            canonical({"title": "Delivery timing"}),
            actor,
        )
        for revision in (1, 2, 3)
    ]
    history = RecoveredHistory(tmp_path / "history.sqlite")
    for index, operation in enumerate(operations):
        history.add_operation(row(operation, f"2026-10-09 01:00:0{index}.000001"))
    for index in (0, 1):
        history.add_operation(row(operations[index], f"2026-10-09 01:00:0{index}.000001"), retained=True)
    path = history.write_stream(tmp_path / "missing.jsonl", missing_only=True)
    replay = [json.loads(line)["operation"] for line in path.read_text().splitlines()]
    assert [operation["event_id"] for operation in replay] == [operations[2].event_id]
    history.close()
    with sqlite3.connect(tmp_path / "history.sqlite") as connection:
        assert connection.execute("SELECT COUNT(*) FROM history").fetchone()[0] == 3


@pytest.mark.parametrize("mode", ["normal", "oversized", "truncated", "timeout", "failed"])
def test_reference_sql_export_is_streamed_bounded_and_has_one_deadline(tmp_path, mode):
    repair = DatabaseReferenceRepair(SimpleNamespace(), tmp_path)
    repair._deadline = time.monotonic() + (0.2 if mode == "timeout" else 5)
    programs = {
        "normal": "import json; [print(json.dumps({'row':i})) for i in range(2000)]",
        "oversized": "print('x'*262145)",
        "truncated": "import sys;sys.stdout.write('{\"row\":1}')",
        "timeout": "import time;time.sleep(5)",
        "failed": "raise SystemExit(1)",
    }
    stream = repair._stream_rows([sys.executable, "-c", programs[mode]])
    if mode == "normal":
        assert sum(1 for _ in stream) == 2000
    else:
        with pytest.raises(TimeoutError if mode == "timeout" else RuntimeError):
            list(stream)


@pytest.mark.skipif(os.name != "posix", reason="Owned process-group cancellation requires Linux")
@pytest.mark.parametrize("action", ["replay", "copy"])
def test_reference_cancel_reaps_actual_owned_blocked_children(tmp_path, monkeypatch, action):
    module = sys.modules["sregym.conductor.scenarios.codehub_reference_repair"]
    cancel, entered = threading.Event(), threading.Event()
    repair = DatabaseReferenceRepair(
        SimpleNamespace(regions=(SimpleNamespace(name="region-a", namespace="codehub-a"),)), tmp_path, cancel=cancel
    )
    repair._deadline = time.monotonic() + 60
    original, children, errors = subprocess.Popen, [], []

    def launch(command, **kwargs):
        if command[0] == "kubectl":
            program = (
                "import sys,time;sys.stdout.write('streamed source\\n');sys.stdout.flush();time.sleep(60)"
                if "mysqldump" in command[-1]
                else "import sys,time;sys.stdin.readline();time.sleep(60)"
            )
            command = [sys.executable, "-c", program]
        child = original(command, **kwargs)
        children.append(child)
        if len(children) == (2 if action == "copy" else 1):
            entered.set()
        return child

    monkeypatch.setattr(module.subprocess, "Popen", launch)

    def run():
        try:
            if action == "replay":
                repair._run_process([sys.executable, "-c", "import time;time.sleep(60)"], stdin=subprocess.DEVNULL)
            else:
                member = SimpleNamespace(region="region-a", origin="mysql://mysql-g0-writer.codehub-a.svc:3306")
                repair._copy_database(member, member, timeout=60)
        except Exception as error:
            errors.append(error)

    worker = threading.Thread(target=run)
    worker.start()
    assert entered.wait(3)
    started = time.monotonic()
    cancel.set()
    worker.join(3)
    assert not worker.is_alive() and time.monotonic() - started < 3
    assert len(errors) == 1 and "cancelled" in str(errors[0])
    assert children and all(child.poll() is not None for child in children)


def test_recovery_spool_budget_refuses_growth_and_preserves_previous_committed_history(tmp_path):
    history = RecoveredHistory(tmp_path / "recovered.sqlite", byte_budget=4 * 1024**2)
    operation = Operation(
        str(uuid4()),
        str(uuid4()),
        str(uuid4()),
        str(uuid4()),
        1,
        "issue.create",
        canonical({"title": "Retained"}),
        str(uuid4()),
    )
    history.add_operation(row(operation, "2026-10-09 01:00:00.000001"))
    history.flush()
    (tmp_path / "retained-copy").write_bytes(b"x" * (4 * 1024**2))
    with pytest.raises(RuntimeError, match="spool capacity"):
        history.write_stream(tmp_path / "recovered.jsonl")
    history.close()
    with sqlite3.connect(tmp_path / "recovered.sqlite") as database:
        assert database.execute("SELECT event_id FROM history").fetchall() == [(operation.event_id,)]


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
    from kubernetes.client import ApiClient, CoreV1Api

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
    module.ApiClient = ApiClient
    module.CoreV1Api = CoreV1Api
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


@pytest.mark.parametrize("second", ["owned-ready", "foreign-ready", "owned-unready", "replaced-replicaset"])
def test_recycle_preserves_last_captured_ready_worker(tmp_path, monkeypatch, second):
    instance, _ledger, _client = controller(tmp_path)
    namespace, deployment_uid = "codehub-a", "worker-deployment"
    instance.app.regions = (SimpleNamespace(name="region-a", namespace=namespace),)
    instance.app.inventory = SimpleNamespace(
        resources=(SimpleNamespace(kind="Deployment", name="worker", namespace=namespace, uid=deployment_uid),)
    )
    instance._assert_namespace = lambda _namespace: None
    deleted = []

    def pod(name, *, ready=True, replica="owned-rs"):
        return SimpleNamespace(
            metadata=SimpleNamespace(
                name=name,
                uid=name + "-uid",
                owner_references=[SimpleNamespace(kind="ReplicaSet", controller=True, name=replica, uid=replica)],
            ),
            status=SimpleNamespace(conditions=[SimpleNamespace(type="Ready", status="True" if ready else "False")]),
        )

    pods = [pod("first"), pod("second", ready=second != "owned-unready", replica="second-rs")]

    def replica(name, *_args, **_kwargs):
        return SimpleNamespace(
            metadata=SimpleNamespace(
                uid="replacement" if second == "replaced-replicaset" and name == "second-rs" else name,
                owner_references=[
                    SimpleNamespace(
                        kind="Deployment",
                        controller=True,
                        uid="foreign" if second == "foreign-ready" and name == "second-rs" else deployment_uid,
                    )
                ],
            )
        )

    api = SimpleNamespace(
        read_namespaced_deployment=lambda *_args, **_kwargs: SimpleNamespace(
            metadata=SimpleNamespace(uid=deployment_uid)
        ),
        read_namespaced_replica_set=replica,
    )
    module = ModuleType("kubernetes.client")
    module.AppsV1Api = lambda _client: api
    monkeypatch.setitem(sys.modules, "kubernetes.client", module)
    instance.app._client = lambda: SimpleNamespace(
        core_v1_api=SimpleNamespace(
            api_client=None,
            list_namespaced_pod=lambda *_args, **_kwargs: SimpleNamespace(items=pods),
            delete_namespaced_pod=lambda *args, **kwargs: deleted.append((args, kwargs)),
        )
    )
    try:
        if second == "owned-ready":
            assert instance._recycle_worker("region-a")["remaining_owned_workers"] == 1
            assert len(deleted) == 1 and deleted[0][1]["body"] == {"preconditions": {"uid": "first-uid"}}
        else:
            with pytest.raises(RuntimeError, match="redundant .*serving workers"):
                instance._recycle_worker("region-a")
            assert deleted == []
    finally:
        instance.stop()


def test_failed_real_noise_invalidates_verification(tmp_path):
    instance, _ledger, _client = controller(tmp_path)
    from sregym.conductor.scenarios.database_recovery import NoiseDecision, NoiseEvent, NoisePlan
    from sregym.generators.noise.codehub import NoiseExecutor

    instance.noise = NoiseExecutor(
        NoisePlan(
            "recovery-noise-v2",
            (NoiseDecision("late-failure", NoiseEvent(0, 1, "region-a/tenant-000", "traffic-burst"), True, None),),
        ),
        lambda *_args: {},
        tmp_path / "noise.jsonl",
    )
    instance.noise.run()
    with pytest.raises(RuntimeError, match="required real-noise"):
        instance.verification_inputs()
    with pytest.raises(RuntimeError, match="required real-noise"):
        instance.traffic_facts()
    instance.stop()


def test_snapshot_keeps_its_cut_while_customer_traffic_continues(tmp_path):
    instance, ledger, _client = controller(tmp_path)
    instance.traffic_step()
    instance._epoch_started -= 2
    instance.traffic_step()
    entered, release = threading.Event(), threading.Event()
    results, errors = [], []

    def builder(inputs, source):
        assert not instance._lock._is_owned() and source is not ledger
        assert inputs.tenants == instance.seed_result.tenants
        assert not hasattr(inputs, "receipt_cut") and not hasattr(inputs, "expected_effects")
        before = source.cut()
        entered.set()
        assert release.wait(3)
        assert source.cut() == before
        return before

    def snapshot():
        try:
            results.append(instance.verification_snapshot(builder))
        except Exception as error:
            errors.append(error)

    thread = threading.Thread(target=snapshot)
    thread.start()
    try:
        assert entered.wait(1)
        instance._epoch_started -= 2
        assert instance.traffic_step()["acknowledged"] == 2
        assert ledger.cut().operations == 4 and thread.is_alive()
        release.set()
        thread.join(2)
        assert not thread.is_alive() and errors == []
        assert results[0].operations == 2 and not instance.cancel.is_set()
    finally:
        release.set()
        thread.join(2)
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

    def mysql(member, query, **_kwargs):
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
    repair._export = lambda _member, history, **kwargs: history.add_operation(
        row(operation, "2026-10-09 01:00:00.000001"), retained=kwargs.get("retained", False)
    )
    repair._copy_database = lambda source, destination, **_kwargs: copies.append((source.name, destination.name))
    repair._restore_worker_route = lambda namespace, group: routes.append((namespace, group))
    repair._run_process = lambda *_args, **_kwargs: None
    result = repair.run()
    assert result["group"] == "group-0" and result["recovered_operations"] == 1
    assert all(name.startswith("g0-") for name, _query in queries)
    assert all(source == "g0-candidate" and destination.startswith("g0-") for source, destination in copies)
    replication = {name: query for name, query in queries if "CHANGE REPLICATION SOURCE" in query}
    assert all("SOURCE_CONNECT_RETRY=1, SOURCE_RETRY_COUNT=600" in query for query in replication.values())
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
    updates, projections = [], []
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
        core_v1_api=core,
        exec_command_checked=lambda *_args, **_kwargs: pytest.fail("Use the bounded projection primitive"),
    )
    app = SimpleNamespace(
        regions=(SimpleNamespace(name="region-b", namespace="codehub-b"),),
        _client=lambda: client,
        await_worker_group_route=lambda *args, **kwargs: projections.append((args, kwargs)),
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
        module,
        "owned_worker_pods",
        lambda *_args, **_kwargs: (SimpleNamespace(metadata=SimpleNamespace(name="worker-owned")),),
    )
    if actual_uid != "captured-config":
        with pytest.raises(RuntimeError, match="ConfigMap ownership changed"):
            rewrite_worker_group_route(app, "region-b", "group-0", "172.17.0.1", 25001)
        assert updates == projections == []
    else:
        rewrite_worker_group_route(app, "region-b", "group-0", "172.17.0.1", 25001)
        assert len(updates) == 1 and updates[0]["metadata"] == {"resourceVersion": "17", "uid": "captured-config"}
        actual = json.loads(updates[0]["data"]["database-routes.json"])
        assert actual["other"] == original["other"]
        assert actual["*"]["writer_host"] == "172.17.0.1"
        assert len(projections) == 1 and projections[0][0] == ("region-b", "group-0", "172.17.0.1", 25001)
        assert 0 < projections[0][1]["timeout_seconds"] <= 90


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

    def mounted(routes, *, timeout_seconds, cancel):
        assert 298 <= timeout_seconds <= 300 and cancel is instance.cancel
        events.append(("mounted", routes))

    instance.app.await_database_routes = mounted
    instance._prepare_worker_route = lambda _writer, **_kwargs: events.append(("pin",))
    instance._routes_ready(SimpleNamespace())
    assert events == [("route", "normal-tenant", "group-1"), ("pin",), ("mounted", {"normal-tenant": "group-1"})]
    instance.stop()


def test_stop_waits_for_owned_prepare_before_closing_evidence(tmp_path, monkeypatch):
    instance, ledger, _client = controller(tmp_path)
    instance.seed_result = None
    instance.app.inventory = SimpleNamespace(phase=LifecyclePhase.HEALTHY)
    instance.app.tier = TIERS["small"]
    instance.app.database_groups = (SimpleNamespace(members=(SimpleNamespace(role="writer"),)),)
    entered, released = threading.Event(), threading.Event()
    closed = []

    class Seeder:
        def __init__(self, **kwargs):
            self.cancel = kwargs["cancel"]

        def seed(self, _tier):
            entered.set()
            assert self.cancel.wait(2)
            ledger._db.execute("SELECT COUNT(*) FROM requests").fetchone()
            released.set()
            raise RuntimeError("Customer preparation cancelled")

    monkeypatch.setattr(controller_module, "CustomerSeeder", Seeder)
    instance.lease = SimpleNamespace(acquire=lambda: None, close=lambda: closed.append(released.is_set()))
    errors = []

    def prepare():
        try:
            instance.prepare(enable_noise=False)
        except RuntimeError as error:
            errors.append(str(error))

    thread = threading.Thread(target=prepare)
    thread.start()
    assert entered.wait(2)
    instance.stop(timeout=3)
    thread.join(timeout=1)
    assert not thread.is_alive() and instance._stopped and instance._prepare_done.is_set()
    assert errors == ["Customer preparation cancelled"] and closed == [True]


def test_healthy_burst_cannot_evict_last_closed_customer_journey(tmp_path):
    instance, ledger, _client = controller(tmp_path)
    try:
        instance.traffic_step()
        instance._epoch_started -= 2
        instance.traffic_step()
        first_closed = instance.traffic_facts()["groups"]["group-0"]["journeys"]
        assert first_closed
        instance.epoch_seconds = 1000
        for _ in range(75):
            instance.traffic_step()
        assert len(instance._traffic_history["group-0"]) == 64
        assert instance.traffic_facts()["groups"]["group-0"]["journeys"] == first_closed
        instance._epoch_started -= 1001
        instance.traffic_step()
        assert instance.traffic_facts()["groups"]["group-0"]["journeys"][0][0] > first_closed[0][0]
    finally:
        instance.stop()


@pytest.mark.parametrize("owner_failure", ["exception", "cancelled", "dead-thread"])
def test_private_traffic_owner_failures_are_unavailable_not_app_stalls(tmp_path, owner_failure):
    instance, ledger, _client = controller(tmp_path)
    try:
        if owner_failure == "exception":
            instance.workload_error = "OSError"
        elif owner_failure == "cancelled":
            instance.cancel.set()
        else:
            instance._threads = [SimpleNamespace(is_alive=lambda: False, join=lambda **_kwargs: None)]
        with pytest.raises(RuntimeError, match="Private customer traffic"):
            instance.traffic_facts()
    finally:
        instance.stop()


def test_undrained_seed_worker_prevents_ledger_and_lease_teardown(tmp_path):
    instance, ledger, _client = controller(tmp_path)
    released, closed = [False], []

    def drain(*, timeout):
        if not released[0]:
            raise RuntimeError("Customer preparation workers did not stop")

    instance._seeder = SimpleNamespace(drain=drain)
    instance.lease = SimpleNamespace(close=lambda: closed.append(True))
    with pytest.raises(RuntimeError, match="cleanup incomplete"):
        instance.stop(timeout=0.01)
    assert not instance._stopped and not closed
    assert ledger._db.execute("SELECT COUNT(*) FROM requests").fetchone() == (0,)
    released[0] = True
    instance.stop()
    assert instance._stopped and closed == [True]


def test_malformed_application_ack_remains_pending_while_other_group_progresses(tmp_path):
    from sregym.generators.workload.codehub import InvalidApplicationReceipt

    instance, ledger, _client = controller(tmp_path)
    selected, healthy = instance.seed_result.tenants[0], add_healthy_group(instance, ledger)

    class InvalidClient:
        def submit(self, *_args, **_kwargs):
            raise InvalidApplicationReceipt("Changed acknowledgment")

        def close(self):
            pass

    instance._clients[selected.tenant_id] = InvalidClient()
    try:
        assert instance.traffic_step(tenant=selected)["acknowledged"] == 0
        assert len(ledger.pending_requests()) == 1 and instance.workload_error is None
        assert instance.traffic_step(tenant=healthy)["acknowledged"] == 2
    finally:
        instance.stop()
