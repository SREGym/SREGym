"""Private task lifecycle contracts, without claiming a live fault campaign."""

import datetime
import hashlib
import json
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from dataclasses import replace
from tempfile import TemporaryFile
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import UUID, uuid4

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from sregym.conductor.problems import regional_database_failover as module
from sregym.conductor.scenarios.codehub_contracts import (
    DatabaseGroupSpec,
    DatabaseMember,
    LifecyclePhase,
    OwnedResource,
    RegionSpec,
    RunInventory,
    ServiceEndpoint,
)
from sregym.generators.workload.codehub import Operation, ReceiptLedger, canonical
from sregym.generators.workload.codehub_seed import (
    RegionEndpoints,
    SeedResult,
    TenantAccount,
    account_subscriptions,
    expected_effects,
)


def uid(value):
    return str(UUID(int=value))


def certificate():
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Unit fixture CA")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.PEM).decode()


class FakeApp:
    def __init__(self, **kwargs):
        self.namespace, self.kwargs = "codehub-region-a", kwargs
        # Four recorded operations keep this lifecycle fixture small; production
        # CodeHub retains the requested 20k/200k/2m scale contract unchanged.
        self.tier = replace(kwargs["tier"], records=4, tenants_per_zone=1)
        self.deployment_owner = "owned-deployment"
        self.regions = tuple(
            RegionSpec(
                f"region-{letter}",
                f"codehub-region-{letter}",
                (f"worker-{letter}",),
                (ServiceEndpoint("api", f"http://api.codehub-region-{letter}.svc:8080"),),
            )
            for letter in "ab"
        )
        run_id = str(uuid4())
        resources = []
        for region in self.regions:
            resources.append(OwnedResource(run_id, "Namespace", "", region.namespace, region.namespace + "-uid"))
            for kind, name in (
                ("Service", "gateway"),
                ("Service", "topology"),
                ("Service", "repository"),
                ("Deployment", "api"),
                ("Deployment", "worker"),
                ("StatefulSet", "queue"),
                ("StatefulSet", "delivery"),
                ("Deployment", "topology"),
                ("StatefulSet", "search"),
                ("StatefulSet", "repository"),
            ):
                resources.append(
                    OwnedResource(run_id, kind, region.namespace, name, region.namespace + "-" + kind + "-" + name)
                )
        self.inventory = RunInventory(run_id, 1, LifecyclePhase.HEALTHY, tuple(resources))
        members = tuple(
            DatabaseMember(
                f"{letter}-{role}", f"region-{letter}", role, f"mysql://db-{role}.codehub-region-{letter}.svc:3306"
            )
            for letter, role in (("a", "writer"), ("a", "reader"), ("b", "candidate"), ("b", "reader"))
        )
        self.database_groups = (DatabaseGroupSpec("group-0", "mysql", members),)
        self.gateway_certificates = {region.namespace: certificate() for region in self.regions}
        self._credentials = {"bootstrap-token": "b" * 40, "service-token": "s" * 40}
        self.observer_password = "o" * 40
        self.cleanup = Mock()
        self.assert_database_forward_owner = Mock()
        core = Mock()
        core.read_namespace.side_effect = lambda name, **_: SimpleNamespace(
            metadata=SimpleNamespace(uid=name + "-uid", labels={"codehub.local/deployment": self.deployment_owner})
        )
        core.read_namespaced_service.side_effect = lambda name, namespace, **_: SimpleNamespace(
            metadata=SimpleNamespace(uid=namespace + "-Service-" + name)
        )
        core.read_namespaced_pod.side_effect = lambda name, namespace, **_: SimpleNamespace(
            metadata=SimpleNamespace(
                owner_references=[
                    SimpleNamespace(
                        kind="StatefulSet", name="search", uid=namespace + "-StatefulSet-search", controller=True
                    )
                ]
            )
        )
        self.client = SimpleNamespace(core_v1_api=core)

    def _client(self):
        return self.client


class FakeObserver:
    def __init__(self, path, *, delivery_address, delivery_port, byte_budget=None):
        self.path, self.address, self.port = path, delivery_address, delivery_port
        self.delivery_url = f"http://{delivery_address}:{delivery_port}/deliveries"
        self.started = self.closed = False

    def start(self):
        self.started = True

    def close(self):
        self.closed = True

    def observations(self, identities):
        return ()


class FakeController:
    def __init__(self, app, private_dir, endpoints, **kwargs):
        self.app, self.endpoints, self.kwargs = app, endpoints, kwargs
        self.ledger = ReceiptLedger(private_dir / "requests.sqlite")
        self.seed_result = None
        self.enable_noise = None
        self.stopped = self.unresolved = False
        self.workload_error = None
        self.stop_error = None

    def prepare(self, *, enable_noise, noise_horizon_seconds=7200):
        self.enable_noise = enable_noise
        self.noise_horizon_seconds = noise_horizon_seconds
        tenants = tuple(
            TenantAccount(
                uid(100 + index),
                uid(200 + index),
                region.name,
                "group-0",
                uid(300 + index),
                "customer-" + str(index) * 40,
                webhook_id=uid(400 + index),
                git_commit="b" * 40,
                git_files=(("app.py", "d" * 64),),
            )
            for index, region in enumerate(self.app.regions)
        )
        epoch = self.ledger.begin_epoch()
        for index, account in enumerate(tenants):
            push = Operation(
                uid(500 + index),
                account.tenant_id,
                uid(600 + index),
                account.project_id,
                1,
                "repository.push",
                canonical({"ref": "refs/heads/main", "commit_sha": "b" * 40}),
                account.owner_id,
            )
            provenance = {
                "project_id": account.project_id,
                "refs": [{"ref": "refs/heads/main", "commit_sha": "b" * 40}],
                "files": [{"commit_sha": "b" * 40, "path": "app.py", "sha256": "d" * 64}],
                "bundles": [{"commit_sha": "b" * 40, "sha256": hashlib.sha256(b"fixture-source").hexdigest()}],
            }
            self.record(push, account, epoch, provenance)
            issue = Operation(
                uid(700 + index),
                account.tenant_id,
                uid(800 + index),
                account.project_id,
                1,
                "issue.create",
                canonical({"title": "Connection timeout", "body": "Review deadline settings", "state": "open"}),
                account.owner_id,
            )
            self.record(issue, account, epoch)
        self.ledger.close_epoch(epoch)
        self.seed_result = SeedResult(tenants, 4, ())

    def record(self, operation, account, epoch, provenance=None):
        effects = expected_effects(operation, account_subscriptions(account, self.kwargs["delivery_url"]))
        self.ledger.request(operation, epoch, effects=effects, provenance=provenance)
        self.ledger.acknowledge(operation.event_id, self.endpoints[account.region].api, 1, 201)

    def resolve_pending(self, **kwargs):
        return not self.unresolved

    def verification_snapshot(self, builder):
        with self.ledger._lock:
            inputs = SimpleNamespace(
                tenants=self.seed_result.tenants, git_provenance_entries=self.ledger.git_provenance_entries()
            )
            return builder(inputs, self.ledger)

    def inject_fault(self):
        return {"fixture": "controller-hook"}

    def reference_repair(self, *, timeout=600):
        return {"fixture": "reference-hook"}

    def stop(self, **kwargs):
        if self.stop_error:
            raise self.stop_error
        if not self.stopped:
            self.ledger.close()
            self.stopped = True


@pytest.fixture
def prepared_problem(tmp_path, monkeypatch):
    problem = module.RegionalDatabaseFailover(
        private_root=tmp_path / "owner",
        app_factory=FakeApp,
        controller_factory=FakeController,
        observer_factory=FakeObserver,
    )

    def endpoints():
        result = {}
        for index, region in enumerate(problem.app.regions):
            ca = problem._write_private(region.name + "-ca.pem", problem.app.gateway_certificates[region.namespace])
            origin = f"https://172.17.0.1:{31000 + index}"
            result[region.name] = RegionEndpoints(origin, origin, f"http://172.17.0.1:{32000 + index}", str(ca))
        return result

    monkeypatch.setattr(problem, "_prepare_endpoints", endpoints)
    problem.prepare_environment(enable_noise=True)
    yield problem
    if problem._controller is not None:
        problem._controller.stop_error = None
    problem.stop_environment()


def test_default_constructor_is_pure_and_predeclares_exact_receiver_policy(tmp_path, monkeypatch):
    constructors = [Mock(side_effect=AssertionError("runtime construction during Problem.__init__")) for _ in range(4)]
    thread_start, connect, socket_create = Mock(), Mock(), Mock()
    monkeypatch.setattr(threading.Thread, "start", thread_start)
    monkeypatch.setattr(sqlite3, "connect", connect)
    monkeypatch.setattr(socket, "socket", socket_create)
    root = tmp_path / "not-created"
    problem = module.RegionalDatabaseFailover(
        private_root=root,
        controller_factory=constructors[0],
        observer_factory=constructors[1],
        journal_factory=constructors[2],
        popen_factory=constructors[3],
    )
    assert not root.exists()
    assert not problem.run_default_workload and not problem.run_default_noise
    assert problem.task_version == module.TASK_VERSION
    assert problem.app.tier.name == "small"
    assert problem.app.webhook_hosts == ("172.17.0.1",)
    assert problem.app.webhook_egress == (("172.17.0.1/32", 30455),)
    for constructor in (*constructors, thread_start, connect, socket_create):
        constructor.assert_not_called()


@pytest.mark.parametrize("tier", ["small", "medium", "large"])
def test_requested_tier_is_retained_without_deployment(tier):
    problem = module.RegionalDatabaseFailover(tier=tier)
    assert problem.app.tier.name == tier
    assert not problem.app.regions and problem._controller is None


@pytest.mark.parametrize("changes", [{"tier": "tiny"}, {"seed": True}, {"seed": -1}, {"readiness_seconds": 0}])
def test_invalid_configuration_fails_before_app_creation(changes):
    factory = Mock()
    with pytest.raises(ValueError):
        module.RegionalDatabaseFailover(app_factory=factory, **changes)
    factory.assert_not_called()


def test_preparation_owns_private_files_receiver_and_continuing_noise(prepared_problem):
    problem = prepared_problem
    assert problem._prepared and problem._observer.started
    assert problem._observer.address == "172.17.0.1" and problem._observer.port == 30455
    assert problem._controller.enable_noise is True
    assert problem._private_dir.is_dir()
    assert json.loads((problem._private_dir / "owner.json").read_text())["run_id"] == problem.app.inventory.run_id
    assert (problem._private_dir / "service-token").read_text() == "s" * 40
    assert not problem.has_regional_links
    with pytest.raises(RuntimeError, match="once"):
        problem.prepare_environment(enable_noise=False)


def test_complete_original_receipt_effect_git_baseline_is_captured_once(prepared_problem):
    problem = prepared_problem
    problem.mitigation_oracle.capture_baseline()
    oracle = problem.mitigation_oracle
    assert problem._baseline_captured
    assert sum(cut.operations for cut in oracle.baseline_cuts) == 4
    assert oracle.effect_cuts[0].count == 10
    assert len(oracle.outcomes.projects) == 2
    assert oracle.outcomes.observer.transport == "private_pipe"
    assert not oracle.outcomes.observer.read_url and not oracle.outcomes.observer.token
    assert oracle.fresh_journal is problem._fresh_journal
    assert problem._fresh_journal.observer is problem._observer
    with pytest.raises(RuntimeError, match="once"):
        problem.capture_baseline()


def test_verification_advances_owner_closed_cuts_without_rebasing_or_stopping_noise(prepared_problem):
    problem = prepared_problem
    problem.capture_baseline()
    oracle, controller = problem.mitigation_oracle, problem._controller
    original = oracle.baseline_cuts
    project_inventory = problem._seed_projects
    epoch = controller.ledger.begin_epoch()
    tenant = controller.seed_result.tenants[0]
    operation = Operation(
        uid(900),
        tenant.tenant_id,
        uid(901),
        tenant.project_id,
        1,
        "issue.create",
        canonical({"title": "Connection reuse", "body": "Review transport behavior", "state": "open"}),
        tenant.owner_id,
    )
    controller.record(operation, tenant, epoch)
    controller.ledger.close_epoch(epoch)
    problem.prepare_verification()
    assert oracle.baseline_cuts is original
    assert oracle.cuts[0].operations == original[0].operations + 1
    assert oracle.cuts[0].closed_epochs == (0, 1)
    assert problem._seed_projects is project_inventory
    assert not controller.stopped and controller.enable_noise is True and problem._observer.started


def test_unresolved_outcomes_fail_closed_without_replacing_baseline(prepared_problem):
    problem = prepared_problem
    problem.capture_baseline()
    original = problem.mitigation_oracle.cuts
    problem._controller.unresolved = True
    with pytest.raises(RuntimeError, match="unresolved"):
        problem.prepare_verification()
    assert problem.mitigation_oracle.cuts is original


def test_seeded_operations_cannot_disappear_from_closed_evidence(prepared_problem):
    problem = prepared_problem
    problem._seed_accepted_operations = 5
    with pytest.raises(RuntimeError, match="omits original"):
        problem.capture_baseline()
    assert not problem._baseline_captured


def test_hook_order_requires_baseline_before_fault_and_injected_fault_before_reference(prepared_problem):
    problem = prepared_problem
    with pytest.raises(RuntimeError, match="baseline"):
        problem.inject_fault()
    with pytest.raises(RuntimeError, match="injected"):
        problem.recover_fault()
    problem.capture_baseline()
    assert problem.inject_fault() == {"fixture": "controller-hook"}
    assert problem.fault_injected
    assert problem.recover_fault() == {"fixture": "reference-hook"}
    assert not problem.fault_injected


def test_declared_reference_repair_deadline_reaches_only_owned_controller(prepared_problem, monkeypatch):
    from unittest.mock import Mock

    problem = prepared_problem
    problem.capture_baseline()
    problem.inject_fault()
    repair = Mock(return_value={"recovered": True})
    monkeypatch.setattr(problem._controller, "reference_repair", repair)
    problem.reference_repair_seconds = 1200
    assert problem.recover_fault() == {"recovered": True}
    repair.assert_called_once_with(timeout=1200)


@pytest.mark.parametrize("tier,capacity", [("small", 2), ("medium", 4), ("large", 8)])
def test_tier_declares_bounded_trusted_worker_capacity(tier, capacity):
    problem = module.RegionalDatabaseFailover(tier=tier, app_factory=FakeApp)
    assert problem.mitigation_oracle.verification_cpu_limit == capacity
    assert problem.mitigation_oracle.verification_memory_gib_limit == capacity
    assert problem.noise_horizon_seconds == {"small": 7200, "medium": 14400, "large": 86400}[tier]


def test_extended_noise_horizon_reaches_owned_schedule_without_pausing_at_verification(prepared_problem):
    assert prepared_problem._controller.noise_horizon_seconds == prepared_problem.noise_horizon_seconds == 7200
    prepared_problem.capture_baseline()
    prepared_problem.prepare_verification()
    assert prepared_problem._controller.enable_noise is True
    assert prepared_problem._controller.noise_horizon_seconds == 7200


@pytest.mark.parametrize("timeout", [True, 0, 3601, "600"])
def test_invalid_reference_repair_budget_refuses_application_construction(timeout):
    def forbidden(**_kwargs):
        pytest.fail("Invalid repair budget must not construct an application")

    with pytest.raises(ValueError, match="Reference repair"):
        module.RegionalDatabaseFailover(reference_repair_seconds=timeout, app_factory=forbidden)


def test_grouped_targets_cover_all_declared_replicas_without_shared_service_masking(prepared_problem):
    inventory = prepared_problem._build_target_inventory()
    assert len(inventory.databases) == 4
    assert {(target.resource_kind, target.expected_replicas) for target in inventory.api_targets} == {("deployment", 2)}
    assert {(target.resource_kind, target.expected_replicas) for target in inventory.search_targets} == {
        ("statefulset", 1)
    }
    assert {(target.resource_kind, target.expected_replicas) for target in inventory.repository_targets} == {
        ("statefulset", 1)
    }


def test_partial_prepare_failure_closes_successfully_created_private_resources(tmp_path, monkeypatch):
    observer = FakeObserver(tmp_path / "unused", delivery_address="172.17.0.1", delivery_port=30455)
    problem = module.RegionalDatabaseFailover(
        private_root=tmp_path / "owner", app_factory=FakeApp, observer_factory=lambda *a, **kw: observer
    )
    monkeypatch.setattr(problem, "_prepare_endpoints", Mock(side_effect=RuntimeError("endpoint startup failed")))
    with pytest.raises(RuntimeError, match="endpoint startup"):
        problem.prepare_environment(enable_noise=True)
    assert observer.started and observer.closed
    assert problem._observer is None and problem._controller is None and not problem._prepared
    problem.app.cleanup.assert_not_called()
    problem.stop_environment()


def test_cleanup_stops_only_owned_services_and_preserves_private_evidence(prepared_problem):
    problem = prepared_problem
    controller, observer, directory = problem._controller, problem._observer, problem._private_dir
    process = Mock()
    process.poll.return_value = None
    log = Mock()
    problem._forwards.append(module._Forward(process, log, "http://172.17.0.1:32111"))
    problem.stop_environment()
    problem.stop_environment()
    assert controller.stopped and observer.closed and directory.is_dir()
    process.terminate.assert_called_once()
    process.wait.assert_called_once_with(timeout=5)
    log.close.assert_called_once()
    problem.app.cleanup.assert_not_called()


@pytest.mark.parametrize("failed", [False, True])
def test_late_noise_error_invalidates_attempt_without_preventing_owned_cleanup(prepared_problem, failed):
    problem = prepared_problem
    problem._controller.noise = SimpleNamespace(
        assert_completed=Mock(side_effect=RuntimeError("Late noise failure") if failed else None)
    )
    problem.stop_environment()
    assert problem._controller is None and problem._observer is None
    assert problem.environment_failure == ("real_noise_owner_failed" if failed else None)


def test_failed_controller_stop_keeps_dependencies_until_retry(prepared_problem):
    problem = prepared_problem
    controller, observer = problem._controller, problem._observer
    controller.stop_error = RuntimeError("traffic did not stop")
    with pytest.raises(ExceptionGroup):
        problem.stop_environment()
    assert problem._controller is controller and not observer.closed
    controller.stop_error = None
    problem.stop_environment()
    assert observer.closed


def test_owned_forward_termination_escalates_only_captured_process():
    process, log = Mock(), Mock()
    process.poll.return_value = None
    process.wait.side_effect = [subprocess.TimeoutExpired("kubectl", 5), None]
    forward = module._Forward(process, log, "http://172.17.0.1:32111")
    forward.close()
    process.terminate.assert_called_once()
    process.kill.assert_called_once()
    assert forward.closed and log.close.call_count == 1


def test_forward_reconnects_owned_process_and_stops_supervisor_without_leaks(tmp_path):
    original = subprocess.Popen([sys.executable, "-c", "pass"])
    original.wait(timeout=5)
    log = (tmp_path / "forward.log").open("wb")
    forward = module._Forward(original, log, "http://127.0.0.1:19000")
    restarted = threading.Event()
    checks = []
    children = []

    def launch():
        child = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(30)"])
        children.append(child)
        restarted.set()
        return child

    try:
        forward.supervise(launch, lambda **_: checks.append("captured-owner-verified"))
        assert restarted.wait(timeout=3)
        assert checks == ["captured-owner-verified"]
    finally:
        forward.close()
    assert len(children) == 1 and children[0].poll() is not None
    assert not forward._thread.is_alive() and forward.closed and log.closed


def test_forward_reconnect_refuses_replaced_owner_before_launch(tmp_path):
    original = subprocess.Popen([sys.executable, "-c", "pass"])
    original.wait(timeout=5)
    log = (tmp_path / "forward.log").open("wb")
    forward = module._Forward(original, log, "http://127.0.0.1:19000")
    launch = Mock()
    rejected = threading.Event()

    def owner(**_):
        rejected.set()
        raise RuntimeError("Namespace ownership changed")

    try:
        forward.supervise(launch, owner)
        assert rejected.wait(timeout=3)
        forward._thread.join(timeout=3)
        assert isinstance(forward.supervisor_error, RuntimeError)
        launch.assert_not_called()
    finally:
        forward.close()


def test_forward_cleanup_reaps_child_and_closes_log_even_if_supervisor_cannot_join(tmp_path):
    process = Mock()
    process.poll.return_value = None
    log = (tmp_path / "forward.log").open("wb")
    forward = module._Forward(process, log, "http://127.0.0.1:19000")
    forward._thread = Mock()
    forward._thread.is_alive.return_value = True
    with pytest.raises(RuntimeError, match="supervisor did not stop"):
        forward.close()
    process.terminate.assert_called_once()
    process.wait.assert_called_once_with(timeout=5)
    assert forward.closed and log.closed and forward._stop.is_set()


def test_namespace_replacement_is_rejected_before_forward_launch(prepared_problem):
    problem = prepared_problem
    problem.app.client.core_v1_api.read_namespace.side_effect = lambda *a, **kw: SimpleNamespace(
        metadata=SimpleNamespace(uid="replacement", labels={})
    )
    problem._popen_factory = Mock()
    with pytest.raises(RuntimeError, match="ownership changed"):
        problem._start_forward(problem.app.regions[0], "gateway", remote_port=8443)
    problem._popen_factory.assert_not_called()


def test_concrete_search_forward_requires_captured_stateful_owner(prepared_problem):
    problem = prepared_problem
    problem.app.client.core_v1_api.read_namespaced_pod.side_effect = lambda *a, **kw: SimpleNamespace(
        metadata=SimpleNamespace(
            owner_references=[SimpleNamespace(kind="StatefulSet", name="search", uid="replacement", controller=True)]
        )
    )
    problem._popen_factory = Mock()
    with pytest.raises(RuntimeError, match="stateful owner"):
        problem._start_forward(problem.app.regions[0], "search-0", resource_kind="pod")
    problem._popen_factory.assert_not_called()


def test_runtime_handle_exclusions_leave_persistent_journal_and_immutable_evidence(prepared_problem):
    problem = prepared_problem
    problem.capture_baseline()
    exclusions = set(problem.verifier_excluded_fields)
    assert {"_controller", "_observer", "_forwards", "_link_binding", "_runtime_lock"} <= exclusions
    assert "_fresh_journal" not in exclusions
    assert "mitigation_oracle" not in exclusions
    assert "_routing" not in exclusions and "_seed_projects" not in exclusions
    assert isinstance(problem._routing, tuple) and isinstance(problem._seed_projects, tuple)


def test_unprepared_oracle_cannot_grade_on_host(monkeypatch):
    problem = module.RegionalDatabaseFailover()
    monkeypatch.delenv("SREGYM_VERIFIER_CONTAINER", raising=False)
    assert problem.mitigation_oracle.evaluate()["reason"] == "recovery_container_required"
    with pytest.raises(RuntimeError, match="original healthy"):
        problem.prepare_verification()


def test_optional_link_binding_is_private_and_stops_owned_relay(prepared_problem):
    problem = prepared_problem
    relay = Mock()
    problem._link_binding = module.RegionalLinkBinding(relay, ("172.17.0.1", 22001), Mock())
    problem.stop_environment()
    relay.stop.assert_called_once()
    assert problem._link_binding is None


def test_link_plan_is_declared_purely_before_app_construction():
    factory = Mock()
    planned = {("region-a", "region-b", "group-0"): 21000, ("region-b", "region-a", "group-0"): 21001}
    factory.database_links.return_value = planned
    problem = module.RegionalDatabaseFailover(app_factory=FakeApp, link_factory=factory)
    factory.database_links.assert_called_once_with(module.TIERS["small"])
    factory.assert_not_called()
    assert problem.app.kwargs["database_links"] == planned
    assert problem._private_dir is None and problem._forwards == []


def test_problem_passes_explicit_storage_class_to_application_without_setup():
    problem = module.RegionalDatabaseFailover(app_factory=FakeApp, storage_class="standard")
    assert problem.app.kwargs["storage_class"] == "standard"
    assert problem._controller is None and problem._private_dir is None


@pytest.mark.parametrize("port", [None, 80, 32768, True])
def test_database_forward_rejects_public_or_unbounded_upstreams_before_launch(prepared_problem, port):
    problem = prepared_problem
    problem._popen_factory = Mock()
    with pytest.raises(ValueError):
        problem._start_forward(problem.app.regions[0], "db-writer", remote_port=3306, http_ready=False, local_port=port)
    with pytest.raises(ValueError):
        problem._start_database_forward(
            problem.app.database_groups[0].members[0], local_port=19000, bind_address="172.17.0.1"
        )
    problem._popen_factory.assert_not_called()


def test_database_forward_is_owned_loopback_and_uses_the_exact_declared_port(prepared_problem, monkeypatch):
    problem = prepared_problem
    member = problem.app.database_groups[0].members[0]
    region = problem.app.regions[0]
    problem.app.inventory = replace(
        problem.app.inventory,
        resources=problem.app.inventory.resources
        + (
            OwnedResource(
                problem.app.inventory.run_id,
                "Service",
                region.namespace,
                "db-writer",
                region.namespace + "-Service-db-writer",
            ),
        ),
    )
    reservation = Mock()
    reservation.__enter__ = Mock(return_value=reservation)
    reservation.__exit__ = Mock(return_value=False)
    reservation.getsockname.return_value = ("127.0.0.1", 19000)
    monkeypatch.setattr(module.socket, "socket", Mock(return_value=reservation))
    connection = Mock()
    connection.__enter__ = Mock(return_value=connection)
    connection.__exit__ = Mock(return_value=False)
    connect = Mock(return_value=connection)
    monkeypatch.setattr(module.socket, "create_connection", connect)
    process = Mock()
    process.poll.return_value = None
    launch = Mock(return_value=process)
    problem._popen_factory = launch
    assert problem._start_database_forward(member, local_port=19000) == ("127.0.0.1", 19000)
    reservation.bind.assert_called_once_with(("127.0.0.1", 19000))
    argv = launch.call_args.args[0]
    assert "19000:3306" in argv and argv[-2:] == ["--address", "127.0.0.1"]
    connect.assert_called_once_with(("127.0.0.1", 19000), timeout=0.2)
    assert len(problem._forwards) == 1
    monkeypatch.undo()


def test_occupied_upstream_port_fails_without_fallback_or_process_launch(prepared_problem, monkeypatch):
    problem = prepared_problem
    member = problem.app.database_groups[0].members[0]
    region = problem.app.regions[0]
    problem.app.inventory = replace(
        problem.app.inventory,
        resources=problem.app.inventory.resources
        + (
            OwnedResource(
                problem.app.inventory.run_id,
                "Service",
                region.namespace,
                "db-writer",
                region.namespace + "-Service-db-writer",
            ),
        ),
    )
    reservation = Mock()
    reservation.__enter__ = Mock(return_value=reservation)
    reservation.__exit__ = Mock(return_value=False)
    reservation.bind.side_effect = OSError("Address already in use")
    monkeypatch.setattr(module.socket, "socket", Mock(return_value=reservation))
    problem._popen_factory = Mock()
    with pytest.raises(OSError, match="already in use"):
        problem._start_database_forward(member, local_port=19000)
    reservation.bind.assert_called_once_with(("127.0.0.1", 19000))
    problem._popen_factory.assert_not_called()


@pytest.mark.parametrize("tier,gib", [("small", 1), ("medium", 4), ("large", 8)])
def test_tier_declares_private_verifier_scratch_without_runtime_io(tier, gib):
    problem = module.RegionalDatabaseFailover(tier=tier, app_factory=FakeApp)
    assert problem.mitigation_oracle.verification_scratch_bytes == gib * 1024**3
    assert problem._controller is None and problem._private_dir is None


def test_stop_signals_active_preparation_before_waiting_for_runtime_lock(tmp_path, monkeypatch):
    import threading

    entered, cancelled, drained = threading.Event(), threading.Event(), threading.Event()
    failures = []

    class PreparingController(FakeController):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.cancel = cancelled

        def prepare(self, **kwargs):
            entered.set()
            assert self.cancel.wait(3), "Stop was blocked behind preparation lock"
            # Simulate an owned bounded request returning before evidence closes.
            self.ledger._db.execute("SELECT COUNT(*) FROM requests").fetchone()
            drained.set()
            raise RuntimeError("Customer preparation cancelled")

        def stop(self, **kwargs):
            assert drained.is_set(), "Ledger teardown raced active preparation"
            return super().stop(**kwargs)

    problem = module.RegionalDatabaseFailover(
        private_root=tmp_path / "owner",
        app_factory=FakeApp,
        controller_factory=PreparingController,
        observer_factory=FakeObserver,
    )
    monkeypatch.setattr(
        problem,
        "_prepare_endpoints",
        lambda: {
            region.name: RegionEndpoints("http://127.0.0.1:1234", "http://127.0.0.1:1234", "http://127.0.0.1:1234")
            for region in problem.app.regions
        },
    )

    def prepare():
        try:
            problem.prepare_environment(enable_noise=False)
        except RuntimeError as error:
            failures.append(str(error))

    thread = threading.Thread(target=prepare, name="active-preparation-control")
    thread.start()
    assert entered.wait(2)
    started = time.monotonic()
    problem.stop_environment()
    thread.join(timeout=2)
    assert time.monotonic() - started < 2 and not thread.is_alive()
    assert cancelled.is_set() and drained.is_set() and failures == ["Customer preparation cancelled"]
    assert problem._controller is problem._observer is None


def test_required_process_inventory_matches_actual_rendered_chart(prepared_problem):
    import shutil
    import subprocess
    from pathlib import Path

    import yaml

    if shutil.which("helm") is None:
        pytest.skip("Actual Helm renderer is required")
    chart = Path(__file__).resolve().parents[2] / "SREGym-applications/codehub/helm"
    rendered = subprocess.check_output(["helm", "template", "codehub", str(chart)], text=True, timeout=20)
    controllers = {
        item["metadata"]["name"]: item["kind"].lower()
        for item in yaml.safe_load_all(rendered)
        if item and item["kind"] in {"Deployment", "StatefulSet"}
    }
    prepared_problem.capture_baseline()
    for target in prepared_problem.mitigation_oracle.outcomes.process_targets:
        assert target.kind == controllers[target.name]


def test_actual_parallel_seeder_stop_retains_all_problem_dependencies_until_drain(tmp_path, monkeypatch):
    from sregym.conductor.scenarios import codehub_controller as control
    from sregym.conductor.scenarios.codehub_controller import OwnerLease, RecoveryController
    from sregym.conductor.scenarios.codehub_observer import DeliveryObserver
    from sregym.generators.workload.codehub_seed import CustomerSeeder

    entered, release = threading.Event(), threading.Event()
    clients, failures = [], []

    class BlockedClient:
        closed = False

        def __init__(self, _origin, _token, ledger):
            self.ledger = ledger
            clients.append(self)

        def submit(self, operation, *, epoch, effects=(), **_kwargs):
            self.ledger.request(operation, epoch, effects=effects)
            entered.set()
            assert release.wait(5)
            assert not self.closed
            self.ledger._db.execute("SELECT COUNT(*) FROM requests").fetchone()
            return False

        def close(self):
            self.closed = True

    class ActualController(RecoveryController):
        def __init__(self, *args, **kwargs):
            kwargs["client_factory"] = BlockedClient
            super().__init__(*args, **kwargs)

        def stop(self, *, timeout=60):
            return super().stop(timeout=min(timeout, 0.05))

    def seeder_factory(**kwargs):
        kwargs["fill_workers"] = 2
        seeder = CustomerSeeder(**kwargs)
        seeder.WORKER_STOP_SECONDS = 0.02
        accounts = tuple(
            TenantAccount(uid(100 + i), uid(200 + i), region, "group-0", uid(300 + i), "customer-" + str(i) * 40)
            for i, region in enumerate(kwargs["endpoints"])
        )
        seeder.seed = lambda _tier: seeder.fill_customer_history(accounts, 6, epoch=seeder.ledger.begin_epoch())
        return seeder

    monkeypatch.setattr(control, "CustomerSeeder", seeder_factory)
    problem = module.RegionalDatabaseFailover(
        private_root=tmp_path / "owner",
        app_factory=FakeApp,
        controller_factory=ActualController,
        observer_factory=lambda path, **kwargs: DeliveryObserver(
            path, delivery_address="127.0.0.1", delivery_port=0, byte_budget=kwargs["byte_budget"]
        ),
    )
    lease = OwnerLease(tmp_path / "application.lock")
    lease.acquire()
    problem.app._environment_lease = lease
    monkeypatch.setattr(
        problem,
        "_prepare_endpoints",
        lambda: {
            region.name: RegionEndpoints("http://127.0.0.1:1234", "http://127.0.0.1:1234", "http://127.0.0.1:1234")
            for region in problem.app.regions
        },
    )
    child = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(60)"], stdout=subprocess.DEVNULL)
    forward = module._Forward(child, TemporaryFile(), "http://127.0.0.1:1234")  # noqa: SIM115 - forward owns this handle
    problem._forwards.append(forward)

    def prepare():
        try:
            problem.prepare_environment(enable_noise=False)
        except Exception as error:
            failures.append(error)

    worker = threading.Thread(target=prepare)
    try:
        worker.start()
        assert entered.wait(3)
        controller, observer = problem._controller, problem._observer
        with pytest.raises(ExceptionGroup, match="cleanup is incomplete"):
            problem.stop_environment()
        worker.join(1)
        assert not worker.is_alive() and failures
        assert problem._controller is controller and problem._observer is observer
        assert controller.ledger.pending_requests() and all(not client.closed for client in clients)
        assert child.poll() is None and not forward.closed
        observer.observations(())
        with pytest.raises(RuntimeError, match="already owns"):
            OwnerLease(tmp_path / "application.lock").acquire()
        release.set()
        problem.stop_environment()
        assert controller._stopped and all(client.closed for client in clients)
        assert problem._controller is problem._observer is None and forward.closed and child.poll() is not None
    finally:
        release.set()
        worker.join(3)
        problem.stop_environment()
        lease.close()
