"""Actual regional Problem snapshots through the trusted OraclePickler.

These are transport tests with a tiny owner receipt history. They do not deploy
an application, evaluate an oracle on the host, or claim a recovery campaign.
"""

import hashlib
import pickle
import threading
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

from sregym.conductor.oracles.regional_database_recovery import (
    DeliveryObserverTarget,
    RegionalDatabaseRecoveryOracle,
    WebhookDestination,
)
from sregym.conductor.problems.regional_database_failover import RegionalDatabaseFailover
from sregym.conductor.scenarios.codehub_expectations import (
    WebhookSubscription,
    build_receipt_cuts,
    build_recovery_outcomes,
    compile_seed_git_inventory,
)
from sregym.conductor.scenarios.codehub_key_inventory import validate_cut_snapshot_budget
from sregym.conductor.scenarios.codehub_observer import DeliveryObserver
from sregym.conductor.scenarios.codehub_regions import REGION_LABEL, ZONE_LABEL, mysql_groups, regional_inventory
from sregym.generators.workload.codehub import Operation, ReceiptLedger, canonical
from sregym.generators.workload.codehub_seed import (
    WEBHOOK_EVENTS,
    TenantAccount,
    account_subscriptions,
    expected_effects,
    source_provenance,
)
from sregym.service.apps.codehub import CodeHub
from sregym.service.codehub_verification_journal import CodeHubVerificationJournal
from sregym.service.verifier_runtime import _resource_call
from sregym.service.verifier_state import IMAGE_ROOT, restore_oracle, snapshot_oracle
from sregym.service.verifier_worker import RemoteWorkload

REPO_ROOT = Path(__file__).resolve().parents[2]


def uid(value):
    return str(UUID(int=value))


def committed_receipt(operation):
    return operation.observed_row() | {
        "record_key": operation.record_key,
        "operation_sha256": hashlib.sha256(canonical(operation.observed_row()).encode()).hexdigest(),
        "region": "region-a",
        "replayed": False,
    }


def restored_snapshot(payload, resources):
    def call(index, operation, args):
        return _resource_call(resources, {"index": index, "op": operation, "args": args})

    return restore_oracle(payload, lambda index: RemoteWorkload(index, call))


def test_actual_codehub_problem_constructor_creates_no_runtime_or_private_files(tmp_path):
    owner_root = tmp_path / "not-created"
    problem = RegionalDatabaseFailover(
        private_root=owner_root, chart_path=REPO_ROOT / "SREGym-applications/codehub/helm"
    )
    assert type(problem.app) is CodeHub
    assert problem.app.kubectl is None
    assert problem._controller is problem._observer is problem._fresh_journal is None
    assert not owner_root.exists()
    assert problem.app.tier.records == 20_000


@pytest.fixture
def regional_snapshot(tmp_path):
    problem = RegionalDatabaseFailover(
        private_root=tmp_path / "owner-runtime",
        chart_path=REPO_ROOT / "SREGym-applications/codehub/helm",
    )
    app = problem.app
    labels = {
        f"worker-{letter}-{node}": {REGION_LABEL: f"region-{letter}", ZONE_LABEL: f"region-{letter}-node-{node}"}
        for letter in "ab"
        for node in range(app.tier.worker_nodes_per_region)
    }
    app.regions = regional_inventory(app.tier, labels)
    app.database_groups = mysql_groups(app.tier, app.regions)
    app._credentials = {"mysql-observer-password": "readonly-" + "o" * 32, "service-token": "service-" + "s" * 32}
    ledger = ReceiptLedger(tmp_path / "owner-only" / "owner-receipts.sqlite")
    observer = DeliveryObserver(tmp_path / "owner-only" / "observer-private.sqlite", delivery_address="127.0.0.1")
    token_path = tmp_path / "owner-only" / "owner-bootstrap.token"
    token_secret = "owner-bootstrap-file-content-excluded"
    token_path.write_text(token_secret, encoding="utf-8")
    token_file = token_path.open(encoding="utf-8")
    source = tmp_path / "independent-source"
    source.mkdir()
    (source / "README.md").write_text("Request router component\n", encoding="utf-8")
    (source / "routing.py").write_text("def route(regions):\n    return sorted(regions)[0]\n", encoding="utf-8")
    source_files = tuple(
        (name, hashlib.sha256((source / name).read_bytes()).hexdigest()) for name in ("README.md", "routing.py")
    )
    accounts = tuple(
        TenantAccount(
            uid(100 + index),
            uid(200 + index),
            region.name,
            "group-0",
            uid(1 + index),
            "customer-" + region.name * 4,
            webhook_id=uid(900 + index),
            git_commit="a" * 40,
            git_files=source_files,
        )
        for index, region in enumerate(app.regions)
    )
    routing = tuple(sorted((account.tenant_id, account.group) for account in accounts))
    delivery_ids = []

    def record(account, entity, kind, payload, event, epoch, provenance=None, project=True):
        operation = Operation(
            uid(event),
            account.tenant_id,
            entity,
            account.project_id if project else None,
            1,
            kind,
            canonical(payload),
            account.owner_id,
        )
        effects = expected_effects(operation, account_subscriptions(account, observer.delivery_url))
        ledger.request(operation, epoch, effects=effects, provenance=provenance)
        ledger.acknowledge(operation.event_id, "http://ordinary-api/v1/operations", 2, 201)
        for effect in effects:
            if effect["effect_kind"] == "delivery":
                observer.append(
                    {
                        "effect_id": effect["effect_id"],
                        "event_id": operation.event_id,
                        "operation": operation.request(),
                    },
                    effect["effect_id"],
                )
                delivery_ids.append(effect["effect_id"])
        return operation

    try:
        epoch = ledger.begin_epoch()
        for index, account in enumerate(accounts):
            record(
                account,
                account.tenant_id,
                "organization.create",
                {"slug": f"harbor-{index}", "name": "Harbor Engineering"},
                1000 + index * 10,
                epoch,
                project=False,
            )
            record(
                account,
                account.project_id,
                "project.create",
                {"slug": "request-router", "name": "Request Router", "default_ref": "refs/heads/main"},
                1001 + index * 10,
                epoch,
            )
            record(
                account,
                account.webhook_id,
                "webhook.create",
                {"url": observer.delivery_url, "events": sorted(WEBHOOK_EVENTS), "enabled": True},
                1002 + index * 10,
                epoch,
            )
            git = source_provenance(source, account.project_id, account.git_commit, "refs/heads/main")
            record(
                account,
                uid(300 + index),
                "repository.push",
                {"ref": "refs/heads/main", "commit_sha": account.git_commit},
                1003 + index * 10,
                epoch,
                git,
            )
            record(
                account,
                uid(400 + index),
                "issue.create",
                {"title": "Retry jitter", "body": "Keep request handling bounded.", "state": "open"},
                1004 + index * 10,
                epoch,
            )
        ledger.close_epoch(epoch)
        seed = compile_seed_git_inventory(accounts, ledger.git_provenance_entries())
        inventory = problem._build_target_inventory()
        hooks = tuple(
            WebhookSubscription(
                account.tenant_id,
                WebhookDestination(account.webhook_id, observer.delivery_url, tuple(sorted(WEBHOOK_EVENTS))),
            )
            for account in accounts
        )
        outcomes = build_recovery_outcomes(
            accounts,
            routing,
            seed_projects=seed,
            git_entries=ledger.git_provenance_entries(),
            inventory=inventory,
            observer=DeliveryObserverTarget("", "", transport="private_pipe"),
            service_token=app._credentials["service-token"],
            webhooks=hooks,
        )
        cuts, effect_cuts = build_receipt_cuts(ledger, routing)
        oracle = problem.mitigation_oracle
        oracle.databases, oracle.cuts, oracle.effect_cuts, oracle.outcomes = (
            inventory.databases,
            cuts,
            effect_cuts,
            outcomes,
        )
        RegionalDatabaseRecoveryOracle.capture_baseline(oracle)
        baseline = oracle.baseline_cuts
        epoch = ledger.begin_epoch()
        record(accounts[0], uid(700), "issue.create", {"title": "Connection reuse", "state": "open"}, 1200, epoch)
        ledger.close_epoch(epoch)
        current, effect_cuts = build_receipt_cuts(ledger, routing)
        oracle.install_verification_snapshot(cuts=current, effect_cuts=effect_cuts, outcomes=outcomes)
        journal = CodeHubVerificationJournal(ledger, dict(routing), observer=observer)
        problem._fresh_journal = oracle.fresh_journal = journal
        problem._routing, problem._seed_projects, problem._target_inventory, problem._webhooks = (
            routing,
            seed,
            inventory,
            hooks,
        )
        problem._baseline_captured = problem._prepared = True
        problem._controller = SimpleNamespace(
            ledger=ledger,
            observer=observer,
            token_path=token_path,
            token_file=token_file,
            marker="owner-controller-state-excluded",
        )
        problem._observer = observer
        problem._forwards = [SimpleNamespace(process=threading.Thread(), diagnostics=token_file)]
        problem._link_binding = SimpleNamespace(owner=threading.Thread(), marker="owner-relay-state-excluded")
        yield SimpleNamespace(
            problem=problem,
            oracle=oracle,
            ledger=ledger,
            observer=observer,
            journal=journal,
            accounts=accounts,
            baseline=baseline,
            current=current,
            delivery_ids=delivery_ids,
            token_secret=token_secret,
        )
    finally:
        token_file.close()
        observer.close()
        ledger.close()


def test_full_problem_snapshot_preserves_exact_evidence_and_keeps_owner_runtime_outside(regional_snapshot):
    state = regional_snapshot
    payload, resources = snapshot_oracle(state.oracle, REPO_ROOT)
    assert len(resources) == 1 and resources[0] is state.journal
    for marker in (
        "owner-receipts.sqlite",
        "observer-private.sqlite",
        "owner-bootstrap.token",
        state.token_secret,
        "owner-controller-state-excluded",
        "owner-relay-state-excluded",
        state.observer.read_token,
    ):
        assert marker.encode() not in payload
    restored = restored_snapshot(payload, resources)
    assert type(restored.problem) is RegionalDatabaseFailover
    assert type(restored.problem.app) is CodeHub
    assert restored.problem.mitigation_oracle is restored
    assert restored.fresh_journal is restored.problem._fresh_journal
    assert type(restored.fresh_journal) is RemoteWorkload
    assert not hasattr(restored.fresh_journal, "ledger") and not hasattr(restored.fresh_journal, "observer")
    assert restored.baseline_cuts == state.baseline and restored.cuts == state.current
    assert restored.baseline_cuts != restored.cuts
    assert all(type(cut.record_keys) is tuple for cut in restored.baseline_cuts + restored.cuts)
    assert restored.outcomes == state.oracle.outcomes and restored.databases == state.oracle.databases
    assert restored.problem._seed_projects == state.problem._seed_projects
    assert restored.problem._controller is restored.problem._observer is restored.problem._forwards is None
    assert restored.problem._link_binding is restored.problem._runtime_lock is None
    assert restored.problem.app.chart_path == IMAGE_ROOT / "SREGym-applications/codehub/helm"
    assert restored.problem.app.config_file == IMAGE_ROOT / "sregym/service/metadata/codehub.json"
    assert state.problem._controller.ledger is state.ledger and state.problem._observer is state.observer
    assert state.ledger.cut().operations == 11


def test_restored_problem_uses_only_the_journal_pipe_and_preserves_acknowledged_owner_history(regional_snapshot):
    state = regional_snapshot
    payload, resources = snapshot_oracle(state.oracle, REPO_ROOT)
    restored = restored_snapshot(payload, resources)
    facts = restored.fresh_journal.delivery_receipts([state.delivery_ids[0]])
    assert facts == {"receipts": list(state.observer.observations((state.delivery_ids[0],)))}
    original_cut = restored.cuts
    account = state.accounts[0]
    operation = Operation(
        uid(1300),
        account.tenant_id,
        uid(800),
        account.project_id,
        1,
        "issue.create",
        canonical({"title": "Deadline propagation", "state": "open"}),
        account.owner_id,
    )
    epoch = restored.fresh_journal.begin_epoch()
    assert restored.fresh_journal.request(
        epoch,
        account.group,
        operation.observed_row(),
        expected_effects(operation, account_subscriptions(account, state.observer.delivery_url)),
    )
    assert restored.fresh_journal.acknowledge(
        epoch, operation.event_id, "http://ordinary-api/v1/operations", 3, 201, committed_receipt(operation)
    )
    assert restored.fresh_journal.close_epoch(epoch)
    assert state.ledger.cut().operations == 12 and not state.ledger.unresolved()
    assert restored.cuts == original_cut  # Owner IO cannot rebase a copied expectation.


def test_full_problem_requires_its_private_resource_adapter_when_restored(regional_snapshot):
    payload, _resources = snapshot_oracle(regional_snapshot.oracle, REPO_ROOT)
    with pytest.raises(pickle.UnpicklingError, match="Unsupported verifier resource"):
        restore_oracle(payload)


@pytest.mark.parametrize("handle", ["sqlite", "thread"])
def test_unadapted_runtime_state_added_to_the_actual_problem_fails_closed(regional_snapshot, handle):
    state = regional_snapshot
    state.problem.unregistered_runtime = state.ledger._db if handle == "sqlite" else threading.Thread()
    with pytest.raises(TypeError):
        snapshot_oracle(state.oracle, REPO_ROOT)
    assert state.ledger.cut().operations == 11


def test_full_problem_oracle_pickler_enforces_the_inventory_frame_guard(regional_snapshot, monkeypatch):
    state = regional_snapshot
    baseline = state.baseline[0]
    keys = tuple(
        sorted(
            set(baseline.record_keys)
            | {f"{state.accounts[0].tenant_id}/{uid(10_000 + index)}" for index in range(4000)}
        )
    )
    state.oracle.baseline_cuts = (replace(baseline, record_keys=keys, operations=len(keys)),)
    state.oracle.cuts = (replace(state.oracle.baseline_cuts[0], closed_epochs=(0, 1)),)
    monkeypatch.setattr(
        "sregym.conductor.oracles.regional_database_recovery.validate_cut_snapshot_budget",
        lambda inventories: validate_cut_snapshot_budget(inventories, max_frame_bytes=512 * 1024, reserved_bytes=0),
    )
    with pytest.raises(ValueError, match="frame budget"):
        snapshot_oracle(state.oracle, REPO_ROOT)
