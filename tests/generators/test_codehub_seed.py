"""Independent private intent and actual local Git customer-journey checks."""

import hashlib
import itertools
import shutil
import subprocess
import threading
from uuid import UUID, uuid4

import pytest

from sregym.conductor.scenarios.database_recovery import ScaleTier
from sregym.generators.workload import codehub_seed as seed_module
from sregym.generators.workload.codehub import (
    Operation,
    ReceiptLedger,
    canonical,
    validate_effects,
    validate_git_provenance,
)
from sregym.generators.workload.codehub_seed import (
    CustomerSeeder,
    RegionEndpoints,
    TenantAccount,
    expected_effects,
    source_provenance,
)


def account():
    return TenantAccount(
        str(uuid4()),
        str(uuid4()),
        "region-a",
        "group-0",
        str(uuid4()),
        "owner" * 10,
        str(uuid4()),
        "reviewer" * 10,
        str(uuid4()),
    )


class AcknowledgingClient:
    def __init__(self, ledger):
        self.ledger = ledger
        self.operations = []

    def submit(self, operation, *, epoch, effects=(), provenance=None, **_kwargs):
        self.ledger.request(operation, epoch, effects=effects, provenance=provenance)
        self.ledger.acknowledge(operation.event_id, "http://customer-api", 1, 201)
        self.operations.append(operation)
        return True

    def close(self):
        pass


def test_independent_effects_exact_subscriptions_and_content():
    tenant = account()
    operation = Operation(
        str(uuid4()),
        tenant.tenant_id,
        str(uuid4()),
        tenant.project_id,
        1,
        "issue.create",
        canonical({"title": "Retry deadline", "state": "open"}),
        tenant.owner_id,
    )
    subscriptions = [
        {"id": tenant.webhook_id, "events": ["issue.create"], "enabled": True, "url": "http://receiver/deliveries"}
    ]
    effects = expected_effects(operation, subscriptions)
    assert {row["effect_kind"] for row in effects} == {"search", "delivery"}
    assert len(validate_effects(operation, effects)) == 2
    disabled = [{**subscriptions[0], "enabled": False}]
    assert [row["effect_kind"] for row in expected_effects(operation, disabled)] == ["search"]


def test_provenance_captured_from_source_before_any_service(tmp_path):
    tenant = account()
    (tmp_path / "routing.py").write_bytes(b"answer = 42\n")
    (tmp_path / "README.md").write_bytes(b"Request routing\n")
    operation = Operation(
        str(uuid4()),
        tenant.tenant_id,
        str(uuid4()),
        tenant.project_id,
        1,
        "repository.push",
        canonical({"ref": "refs/heads/main", "commit_sha": "a" * 40}),
        tenant.owner_id,
    )
    provenance = source_provenance(tmp_path, tenant.project_id, "a" * 40, "refs/heads/main")
    assert validate_git_provenance(operation, provenance) == provenance
    before = provenance["bundles"][0]["sha256"]
    (tmp_path / "routing.py").write_bytes(b"answer = 43\n")
    after = source_provenance(tmp_path, tenant.project_id, "a" * 40, "refs/heads/main")
    assert before != after["bundles"][0]["sha256"]


@pytest.mark.skipif(shutil.which("git") is None, reason="Actual Git executable required")
def test_actual_customer_git_history_and_immutable_preupload_provenance(tmp_path):
    tenant = account()
    remote = tmp_path / "git" / tenant.project_id
    remote.parent.mkdir()
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    ledger = ReceiptLedger(tmp_path / "receipts.sqlite")
    owner, reviewer = AcknowledgingClient(ledger), AcknowledgingClient(ledger)
    seeder = CustomerSeeder(
        endpoints={"region-a": RegionEndpoints("http://api", str(tmp_path), "http://topology")},
        ledger=ledger,
        bootstrap_token="b" * 40,
        delivery_url="http://receiver/deliveries",
        seed=1,
    )
    seeder.subscriptions[(tenant.tenant_id, tenant.project_id)] = [
        {
            "id": tenant.webhook_id,
            "url": "http://receiver/deliveries",
            "enabled": True,
            "events": ["repository.push", "change.create", "change.update"],
        }
    ]
    recovered = seeder.repository_journey(tenant, owner, reviewer, epoch=ledger.begin_epoch())
    ledger.close_epoch(0)
    entries = ledger.git_provenance_entries()
    assert len(entries) == 3
    assert sorted(row["operation"]["client_revision"] for row in entries) == [1, 1, 2]
    assert len({row["provenance"]["bundles"][0]["commit_sha"] for row in entries}) == 2
    refs = subprocess.run(["git", "-C", str(remote), "show-ref"], check=True, capture_output=True, text=True).stdout
    assert recovered.git_commit + " refs/heads/main" in refs
    content = subprocess.run(
        ["git", "-C", str(remote), "show", recovered.git_commit + ":routing.py"], check=True, capture_output=True
    ).stdout
    assert hashlib.sha256(content).hexdigest() == dict(recovered.git_files)["routing.py"]
    assert b"No healthy destination" in content
    assert len(ledger.expected_effects({tenant.tenant_id: tenant.group})[tenant.group]) == 16
    ledger.close()


@pytest.mark.skipif(shutil.which("git") is None, reason="Actual Git executable required")
def test_all_legitimate_group_routes_exist_before_identity_or_create(tmp_path):
    ledger = ReceiptLedger(tmp_path / "receipts.sqlite")
    configured, provisioned = {}, []

    class ProjectClient(AcknowledgingClient):
        def submit(self, operation, **kwargs):
            assert len(configured) == 4
            if operation.kind == "project.create":
                remote = tmp_path / "git" / operation.project_id
                remote.parent.mkdir(exist_ok=True)
                subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
            return super().submit(operation, **kwargs)

    def client_factory(_origin, _token, source):
        return ProjectClient(source)

    endpoints = {
        f"region-{letter}": RegionEndpoints(f"http://api-{letter}", str(tmp_path), f"http://topology-{letter}")
        for letter in "ba"
    }
    seeder = CustomerSeeder(
        endpoints=endpoints,
        ledger=ledger,
        bootstrap_token="b" * 40,
        delivery_url="http://receiver/deliveries",
        seed=1,
        client_factory=client_factory,
        route_tenant=lambda tenant, group: configured.update({tenant: group}),
        routes_ready=lambda: provisioned.append(("routes-ready", len(configured))),
    )

    def provision(_endpoints, user_id, _username, _token):
        assert len(configured) == 4
        provisioned.append((user_id, len(configured)))

    seeder.provision_user = provision
    tier = ScaleTier("medium", 2, 2, 2, 1, 2, 52, 16, 32, 40, database_groups=2)
    result = seeder.seed(tier)
    assert provisioned[0] == ("routes-ready", 4)
    assert result.accepted_operations == 52 and ledger.cut().operations == 52
    assert {account.group for account in result.tenants} == {"group-0", "group-1"}
    assert [account.region for account in result.tenants] == ["region-a", "region-a", "region-b", "region-b"]
    cuts = ledger.partition_cuts(configured)
    assert cuts["group-0"].operations == cuts["group-1"].operations == 26
    ledger.close()


@pytest.mark.parametrize("account_count", [6, 96])
@pytest.mark.parametrize("remaining", [0, 1, 2])
@pytest.mark.parametrize("workers", [1, 4, 16])
def test_bulk_customer_journeys_visit_every_tenant_and_preserve_exact_tail(tmp_path, account_count, remaining, workers):
    tenants = tuple(account() for _ in range(account_count))
    clients = []
    ledger = ReceiptLedger(tmp_path / "receipts.sqlite")

    class FillerClient(AcknowledgingClient):
        closed = False

        def close(self):
            self.closed = True

    def client_factory(_origin, _token, source):
        client = FillerClient(source)
        clients.append(client)
        return client

    seeder = CustomerSeeder(
        endpoints={"region-a": RegionEndpoints("http://api", "http://git", "http://topology")},
        ledger=ledger,
        bootstrap_token="b" * 40,
        delivery_url="http://receiver/deliveries",
        seed=1,
        client_factory=client_factory,
        fill_workers=workers,
    )
    count = account_count * 3 + remaining
    epoch = ledger.begin_epoch()
    try:
        seeder.fill_customer_history(tenants, count, epoch=epoch)
        ledger.close_epoch(epoch)
        assert seeder.accepted == ledger.cut().operations == count
        assert len(clients) == account_count and all(client.closed for client in clients)
        assert len({operation.event_id for client in clients for operation in client.operations}) == count
        for index, (tenant, client) in enumerate(zip(tenants, clients, strict=True)):
            operations = client.operations
            assert all(operation.tenant_id == tenant.tenant_id for operation in operations)
            assert len(operations) == 3 + (remaining if index == 0 else 0)
            create, comment, update = operations[:3]
            assert [operation.kind for operation in (create, comment, update)] == [
                "issue.create",
                "comment.create",
                "issue.update",
            ]
            assert create.entity_id == update.entity_id and create.client_revision == 1 and update.client_revision == 2
            assert comment.request()["payload"]["issue_id"] == create.entity_id
            assert update.request()["payload"]["state"] == "closed"
            if index == 0 and remaining:
                tail = operations[3:]
                assert [operation.kind for operation in tail] == ["issue.create", "comment.create"][:remaining]
                assert tail[0].entity_id != create.entity_id
                if remaining == 2:
                    assert tail[1].request()["payload"]["issue_id"] == tail[0].entity_id
    finally:
        ledger.close()


def history_seeder(ledger, factory, *, workers=2):
    return CustomerSeeder(
        endpoints={"region-a": RegionEndpoints("http://api", "http://git", "http://topology")},
        ledger=ledger,
        bootstrap_token="b" * 40,
        delivery_url="http://receiver/deliveries",
        seed=19,
        client_factory=factory,
        fill_workers=workers,
    )


@pytest.mark.parametrize("workers", [True, 0, -1, 17, 1.5])
def test_bulk_worker_count_is_explicitly_bounded_before_any_network(workers):
    with pytest.raises(ValueError, match="1..16 workers"):
        history_seeder(None, lambda *_args: pytest.fail("No client should be created"), workers=workers)


def test_independent_tenants_overlap_but_each_client_has_one_worker_and_closes_after_join(tmp_path, monkeypatch):
    ledger = ReceiptLedger(tmp_path / "receipts.sqlite")
    tenants = tuple(account() for _ in range(4))
    clients, queues = [], []
    owner_thread = threading.get_ident()
    barrier = threading.Barrier(2)
    active_lock = threading.Lock()
    active, peak = 0, 0
    real_queue = seed_module.Queue

    def tracked_queue(*args, **kwargs):
        queue = real_queue(*args, **kwargs)
        queues.append(queue)
        return queue

    monkeypatch.setattr(seed_module, "Queue", tracked_queue)

    class ConcurrentClient(AcknowledgingClient):
        closed = False

        def __init__(self, source, ordinal):
            super().__init__(source)
            self.ordinal, self.worker_ids = ordinal, set()

        def submit(self, operation, *, epoch, effects=(), provenance=None, **_kwargs):
            nonlocal active, peak
            assert not self.closed
            self.worker_ids.add(threading.get_ident())
            self.ledger.request(operation, epoch, effects=effects, provenance=provenance)
            with active_lock:
                active += 1
                peak = max(peak, active)
            try:
                if self.ordinal < 2 and not self.operations:
                    barrier.wait(timeout=5)
                self.ledger.acknowledge(operation.event_id, "http://api", 1, 201)
                self.operations.append(operation)
                return True
            finally:
                with active_lock:
                    active -= 1

        def close(self):
            assert threading.get_ident() == owner_thread
            assert not any(thread.name.startswith("customer-history-") for thread in threading.enumerate())
            self.closed = True

    def factory(_origin, _token, source):
        client = ConcurrentClient(source, len(clients))
        clients.append(client)
        return client

    seeder = history_seeder(ledger, factory)
    original = seeder.operation

    def planned(*args, **kwargs):
        assert threading.get_ident() == owner_thread
        return original(*args, **kwargs)

    monkeypatch.setattr(seeder, "operation", planned)
    epoch = ledger.begin_epoch()
    seeder.fill_customer_history(tenants, 38, epoch=epoch)
    assert peak == 2 and active == 0
    assert len(queues) == 2 and all(queue.maxsize == 2 and queue.unfinished_tasks == 0 for queue in queues)
    assert all(client.closed and len(client.worker_ids) == 1 for client in clients)
    assert seeder.accepted == 38
    ledger.close_epoch(epoch)
    assert ledger.cut().operations == 38
    ledger.close()


def test_planned_randomness_and_immutable_ids_are_independent_of_completion_order(tmp_path, monkeypatch):
    tenants = tuple(account() for _ in range(6))
    owner_thread = threading.get_ident()

    def run(workers):
        identifiers = itertools.count(1)

        def identity():
            assert threading.get_ident() == owner_thread
            return UUID(int=next(identifiers))

        monkeypatch.setattr(seed_module, "uuid4", identity)
        ledger = ReceiptLedger(tmp_path / f"receipts-{workers}.sqlite")
        clients = []
        other_completed = threading.Event()

        class ReorderedClient(AcknowledgingClient):
            def submit(self, operation, **kwargs):
                if workers > 1 and self is clients[0] and not self.operations:
                    assert other_completed.wait(5)
                result = super().submit(operation, **kwargs)
                if self is clients[1] and operation.kind == "issue.update":
                    other_completed.set()
                return result

        def factory(_origin, _token, source):
            client = ReorderedClient(source)
            clients.append(client)
            return client

        seeder = history_seeder(ledger, factory, workers=workers)
        epoch = ledger.begin_epoch()
        seeder.fill_customer_history(tenants, 56, epoch=epoch)
        ledger.close_epoch(epoch)
        result = (
            sorted(
                (operation.observed_row() for client in clients for operation in client.operations),
                key=lambda row: row["event_id"],
            ),
            seeder.random.getstate(),
        )
        ledger.close()
        return result

    assert run(1) == run(2)


@pytest.mark.skipif(shutil.which("git") is None, reason="Actual Git executable required")
def test_parallel_fill_failure_keeps_seed_epoch_open_and_closes_all_joined_clients(tmp_path):
    ledger = ReceiptLedger(tmp_path / "receipts.sqlite")
    clients = []

    class FailingFillClient(AcknowledgingClient):
        closed = False

        def submit(self, operation, *, epoch, effects=(), provenance=None, **kwargs):
            if operation.kind == "project.create":
                remote = tmp_path / "git" / operation.project_id
                remote.parent.mkdir(exist_ok=True)
                subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
            if operation.kind == "issue.create" and "for gateway change" in operation.request()["payload"]["title"]:
                self.ledger.request(operation, epoch, effects=effects, provenance=provenance)
                return False
            return super().submit(operation, epoch=epoch, effects=effects, provenance=provenance, **kwargs)

        def close(self):
            assert not any(thread.name.startswith("customer-history-") for thread in threading.enumerate())
            self.closed = True

    def factory(_origin, _token, source):
        client = FailingFillClient(source)
        clients.append(client)
        return client

    seeder = history_seeder(ledger, factory, workers=4)
    seeder.endpoints = {
        f"region-{letter}": RegionEndpoints(f"http://api-{letter}", str(tmp_path), f"http://topology-{letter}")
        for letter in "ab"
    }
    seeder.provision_user = lambda *_args: None
    tier = ScaleTier("small", 2, 2, 2, 1, 1, 38, 16, 32, 40)
    with pytest.raises(RuntimeError, match="not acknowledged"):
        seeder.seed(tier)
    assert clients and all(client.closed for client in clients)
    assert ledger.pending_requests() and ledger.cut().operations == 0
    assert ledger._db.execute("SELECT closed FROM epochs").fetchall() == [(0,)]
    assert seeder.accepted == 26
    ledger.close()


def test_partial_client_creation_closes_existing_clients_before_failure(tmp_path):
    ledger = ReceiptLedger(tmp_path / "receipts.sqlite")
    clients = []

    class Client(AcknowledgingClient):
        closed = False

        def close(self):
            self.closed = True

    def factory(_origin, _token, source):
        if clients:
            raise RuntimeError("Client creation failed")
        client = Client(source)
        clients.append(client)
        return client

    seeder = history_seeder(ledger, factory)
    with pytest.raises(RuntimeError, match="Client creation failed"):
        seeder.fill_customer_history((account(), account()), 6, epoch=ledger.begin_epoch())
    assert clients[0].closed and seeder.accepted == 0
    ledger.close()


def test_partial_worker_start_joins_started_thread_and_closes_clients(tmp_path, monkeypatch):
    ledger = ReceiptLedger(tmp_path / "receipts.sqlite")
    clients, started = [], []

    class Client(AcknowledgingClient):
        closed = False

        def close(self):
            assert all(not thread.is_alive() for thread in started)
            self.closed = True

    def factory(_origin, _token, source):
        client = Client(source)
        clients.append(client)
        return client

    def thread_factory(*args, **kwargs):
        if started:
            raise RuntimeError("Worker creation failed")
        thread = threading.Thread(*args, **kwargs)
        started.append(thread)
        return thread

    monkeypatch.setattr(seed_module, "Thread", thread_factory)
    seeder = history_seeder(ledger, factory, workers=3)
    with pytest.raises(RuntimeError, match="Worker creation failed"):
        seeder.fill_customer_history(tuple(account() for _ in range(3)), 9, epoch=ledger.begin_epoch())
    assert all(client.closed for client in clients) and seeder.accepted == 0
    assert len(started) == 1 and not started[0].is_alive()
    ledger.close()
