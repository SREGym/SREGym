"""Private regional recovery task integration, pending live qualification.

This module is deliberately absent from the problem catalog until the real
healthy/fault/reference and isolation campaigns pass. It carries no workload
incident instructions or reward endpoint. Logical regions share one host.
"""

from __future__ import annotations

import json
import os
import socket
import ssl
import stat
import subprocess
import tempfile
import threading
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

from sregym.conductor.oracles.regional_database_recovery import RegionalDatabaseRecoveryOracle
from sregym.conductor.problems.base import Problem
from sregym.conductor.scenarios.codehub_contracts import LifecyclePhase
from sregym.conductor.scenarios.database_recovery import TIERS
from sregym.service.apps.codehub import CodeHub

RUNNER_ADDRESS = "172.17.0.1"
DELIVERY_PORT = 30455
TASK_VERSION = "regional-database-failover-v1"


@dataclass(frozen=True)
class RegionalLinkBinding:
    """Private optional factory result after ordinary DB routes are installed.

    The factory owns only its trusted relay. Its upstream forwards must be
    obtained from the problem's owned forward helper. It installs normal probe
    and replication host/port settings before returning; the fault adapter's
    apply/restore affect the declared links. Presence is not a fidelity claim.
    """

    owner: object
    writer_endpoint: tuple[str, int]
    network_factory: Callable


@dataclass
class _Forward:
    process: object
    diagnostics: object
    origin: str
    closed: bool = False
    supervisor_error: BaseException | None = None
    _stop: object = field(default_factory=threading.Event, repr=False)
    _thread: object = field(default=None, repr=False)

    def supervise(self, relaunch, assert_owner):
        """Reconnect the same owned service/port after an ordinary pod restart."""
        if self._thread is not None or self.closed:
            raise RuntimeError("Owned forward supervision already started or closed")

        def work():
            while not self._stop.wait(0.25):
                if self.process.poll() is None:
                    continue
                try:
                    assert_owner(deadline=time.monotonic() + 10, cancelled=self._stop)
                    if self._stop.is_set():
                        return
                    child = relaunch()
                    self.process = child
                    if self._stop.is_set():
                        self._reap(child)
                        return
                except BaseException as error:
                    if not self._stop.is_set():
                        self.supervisor_error = error
                    return

        self._thread = threading.Thread(target=work, name="owned-service-forward", daemon=False)
        self._thread.start()

    def close(self):
        if self.closed:
            return
        self._stop.set()
        shutdown_error = None
        if self._thread is not None:
            self._thread.join(timeout=15)
            if self._thread.is_alive():
                shutdown_error = RuntimeError("Owned forward supervisor did not stop")
        try:
            self._reap(self.process)
        finally:
            self.diagnostics.close()
            self.closed = True
        if shutdown_error is not None:
            raise shutdown_error

    @staticmethod
    def _reap(process):
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


class _RecoveryOracle(RegionalDatabaseRecoveryOracle):
    def capture_baseline(self):
        self.problem.capture_baseline()


class RegionalDatabaseFailover(Problem):
    run_default_workload = False
    run_default_noise = False
    requires_healthy_verification = True
    verifier_excluded_fields = (
        "_controller",
        "_observer",
        "_forwards",
        "_link_binding",
        "_runtime_lock",
        "_environment_cancel",
        "_controller_factory",
        "_observer_factory",
        "_journal_factory",
        "_link_factory",
        "_popen_factory",
    )

    def __init__(
        self,
        *,
        tier: str = "small",
        seed: int = 1,
        image: str = "codehub:local",
        storage_class: str | None = None,
        chart_path: Path | None = None,
        private_root: Path | None = None,
        lease_path: Path | None = None,
        readiness_seconds: int = 30,
        stable_seconds: int = 60,
        verification_seconds: int = 600,
        reference_repair_seconds: int = 600,
        noise_horizon_seconds: int | None = None,
        foreign_memory_reserve_gib: int = 8,
        app_factory: Callable = CodeHub,
        controller_factory: Callable | None = None,
        observer_factory: Callable | None = None,
        journal_factory: Callable | None = None,
        link_factory: Callable | None = None,
        popen_factory: Callable = subprocess.Popen,
    ):
        if tier not in TIERS:
            raise ValueError("Unknown regional scale tier")
        if type(seed) is not int or seed < 0:
            raise ValueError("Seed must be a nonnegative integer")
        if type(readiness_seconds) is not int or not 1 <= readiness_seconds <= 120:
            raise ValueError("Forward readiness must be bounded to 1..120 seconds")
        if type(reference_repair_seconds) is not int or not 1 <= reference_repair_seconds <= 3600:
            raise ValueError("Reference repair must be bounded to 1..3600 seconds")
        self.reference_repair_seconds = reference_repair_seconds
        if type(foreign_memory_reserve_gib) is not int or not 0 <= foreign_memory_reserve_gib <= 1024:
            raise ValueError("Foreign memory reservation must be bounded to 0..1024 GiB")
        self.noise_horizon_seconds = (
            {"small": 7200, "medium": 14400, "large": 86400}[tier]
            if noise_horizon_seconds is None
            else noise_horizon_seconds
        )
        if type(self.noise_horizon_seconds) is not int or not 15 <= self.noise_horizon_seconds <= 86400:
            raise ValueError("Noise horizon must be bounded to 15..86400 seconds")
        self.task_version, self.seed = TASK_VERSION, seed
        self.private_root = Path(private_root) if private_root is not None else None
        self.lease_path = Path(lease_path) if lease_path is not None else None
        self.readiness_seconds = readiness_seconds
        app = app_factory(
            tier=TIERS[tier],
            image=image,
            storage_class=storage_class,
            chart_path=chart_path,
            webhook_hosts=(RUNNER_ADDRESS,),
            webhook_egress=((f"{RUNNER_ADDRESS}/32", DELIVERY_PORT),),
            database_links=(
                link_factory.database_links(TIERS[tier]) if hasattr(link_factory, "database_links") else {}
            ),
            lease_path=self.lease_path
            or (self.private_root or Path(tempfile.gettempdir()) / "codehub-runs") / "campaign.lock",
            owner_storage_root=self.private_root or Path(tempfile.gettempdir()) / "codehub-runs",
            native_memory_admission=tier == "large",
            foreign_memory_reserve_gib=foreign_memory_reserve_gib,
        )
        super().__init__(app)
        self.root_cause = self.build_structured_root_cause(
            component="database",
            namespace=self.namespace,
            description="Regional writers diverged and dependent work stopped converging.",
        )
        # No controller, ledger, listener, subprocess or filesystem is created here.
        self._controller_factory, self._observer_factory = controller_factory, observer_factory
        self._journal_factory, self._link_factory = journal_factory, link_factory
        self._popen_factory = popen_factory
        self._environment_cancel = threading.Event()
        self._controller = self._observer = self._link_binding = self._fresh_journal = None
        self._forwards: list[_Forward] = []
        self._runtime_lock = threading.RLock()
        self._private_dir: Path | None = None
        self._prepared = self._baseline_captured = False
        self._routing = self._seed_projects = self._webhooks = ()
        self._target_inventory = None
        self._seed_accepted_operations = 0
        self.has_regional_links = False
        self.mitigation_oracle = _RecoveryOracle(
            self,
            stable_seconds=stable_seconds,
            deadline_seconds=verification_seconds,
            verification_scratch_bytes={"small": 1, "medium": 4, "large": 8}[tier] * 1024**3,
        )
        self.mitigation_oracle.verification_cpu_limit = {"small": 2, "medium": 4, "large": 8}[tier]
        self.mitigation_oracle.verification_memory_gib_limit = {"small": 2, "medium": 4, "large": 8}[tier]

    def _create_private_directory(self):
        root = self.private_root or Path(tempfile.gettempdir()) / "codehub-runs"
        if not root.is_absolute() or root.is_symlink():
            raise RuntimeError("Private run root must be an absolute owned directory")
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = root.stat()
        if os.name == "posix" and (info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700):
            raise RuntimeError("Private run root must belong to the runner with mode 0700")
        directory = root / f"{self.app.inventory.run_id}-{uuid4().hex}"
        directory.mkdir(mode=0o700)
        self._private_dir = directory
        self._write_private("owner.json", json.dumps({"run_id": self.app.inventory.run_id}))

    def _write_private(self, name: str, content: str) -> Path:
        if self._private_dir is None or Path(name).name != name:
            raise RuntimeError("An owned private directory and simple filename are required")
        path = self._private_dir / name
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            file.write(content)
        return path

    @staticmethod
    def _forward_timeout(deadline, cancelled):
        if cancelled is not None and cancelled.is_set():
            raise RuntimeError("Owned forward validation cancelled")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Owned forward validation deadline exceeded")
        return min(5, remaining)

    def _assert_namespace(self, namespace, *, deadline=None, cancelled=None):
        resource = next(
            (r for r in self.app.inventory.resources if r.kind == "Namespace" and r.name == namespace), None
        )
        if resource is None:
            raise RuntimeError("Forward namespace is absent from the owned inventory")
        deadline = deadline if deadline is not None else time.monotonic() + 10
        current = self.app._client().core_v1_api.read_namespace(
            namespace, _request_timeout=self._forward_timeout(deadline, cancelled)
        )
        if (
            current.metadata.uid != resource.uid
            or (current.metadata.labels or {}).get("codehub.local/deployment") != self.app.deployment_owner
        ):
            raise RuntimeError("Forward namespace ownership changed")

    def _assert_forward_resource(self, region, resource_kind, name, *, deadline=None, cancelled=None):
        deadline = deadline if deadline is not None else time.monotonic() + 10
        self._assert_namespace(region.namespace, deadline=deadline, cancelled=cancelled)
        core = self.app._client().core_v1_api
        if resource_kind == "pod":
            owner = next(
                (
                    r
                    for r in self.app.inventory.resources
                    if r.namespace == region.namespace and r.kind == "StatefulSet" and r.name == "search"
                ),
                None,
            )
            pod = core.read_namespaced_pod(
                name, region.namespace, _request_timeout=self._forward_timeout(deadline, cancelled)
            )
            if owner is None or not any(
                reference.kind == "StatefulSet"
                and reference.name == "search"
                and reference.uid == owner.uid
                and reference.controller
                for reference in pod.metadata.owner_references or ()
            ):
                raise RuntimeError("Concrete search pod is outside the captured stateful owner")
            return
        if resource_kind != "service":
            raise ValueError("Owner forwards target declared services or concrete search pods")
        resource = next(
            (
                r
                for r in self.app.inventory.resources
                if r.namespace == region.namespace and r.kind == "Service" and r.name == name
            ),
            None,
        )
        current = core.read_namespaced_service(
            name, region.namespace, _request_timeout=self._forward_timeout(deadline, cancelled)
        )
        if resource is None or current.metadata.uid != resource.uid:
            raise RuntimeError("Forward service ownership changed")
        for group in self.app.database_groups:
            for member in group.members:
                if member.region == region.name and member.origin.removeprefix("mysql://").split(".", 1)[0] == name:
                    self.app.assert_database_forward_owner(
                        member, deadline=deadline, cancelled=cancelled, checked_service=current
                    )

    def _start_forward(
        self,
        region,
        name,
        *,
        resource_kind="service",
        remote_port=8080,
        ca_file: Path | None = None,
        http_ready=True,
        bind_address=RUNNER_ADDRESS,
        local_port=None,
    ) -> str:
        """Only captured namespaces/resources; process handles stay on the owner."""
        if type(remote_port) is not int or not 1 <= remote_port <= 65535:
            raise ValueError("Invalid declared forwarding port")
        if bind_address not in {RUNNER_ADDRESS, "127.0.0.1"}:
            raise ValueError("Owner forwards require an explicit runner or loopback bind")
        if local_port is not None and (type(local_port) is not int or not 1024 <= local_port <= 32767):
            raise ValueError("Explicit owner ports must be bounded nonprivileged integers")
        if remote_port == 3306 and (bind_address != "127.0.0.1" or local_port is None or http_ready):
            raise ValueError("Database upstream forwards require loopback and an explicit bounded TCP port")
        self._assert_forward_resource(region, resource_kind, name)
        with socket.socket() as reservation:
            reservation.bind((bind_address, local_port or 0))
            port = reservation.getsockname()[1]
        assert self._private_dir is not None
        path = self._private_dir / f"forward-{len(self._forwards)}.log"
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        diagnostics = os.fdopen(descriptor, "wb")
        command = [
            "kubectl",
            "--request-timeout=10s",
            "-n",
            region.namespace,
            "port-forward",
            f"{resource_kind}/{name}",
            f"{port}:{remote_port}",
            "--address",
            bind_address,
        ]

        def launch():
            return self._popen_factory(
                command,
                stdin=subprocess.DEVNULL,
                stdout=diagnostics,
                stderr=diagnostics,
            )

        try:
            process = launch()
        except BaseException:
            diagnostics.close()
            raise
        origin = f"{'https' if ca_file else 'http'}://{bind_address}:{port}"
        forward = _Forward(process, diagnostics, origin)
        self._forwards.append(forward)
        deadline = time.monotonic() + self.readiness_seconds
        context = ssl.create_default_context(cafile=str(ca_file)) if ca_file is not None else True
        with httpx.Client(verify=context, timeout=2, follow_redirects=False, trust_env=False) as client:
            while time.monotonic() < deadline:
                if self._environment_cancel.is_set():
                    raise RuntimeError("Owned preparation cancelled")
                if process.poll() is not None:
                    raise RuntimeError(f"Owned {name} port-forward exited before readiness")
                try:
                    if http_ready:
                        response = client.get(origin + "/readyz")
                        response.raise_for_status()
                    else:
                        with socket.create_connection((bind_address, port), timeout=0.2):
                            pass
                    if resource_kind == "service":
                        forward.supervise(
                            launch,
                            lambda **limits: self._assert_forward_resource(region, resource_kind, name, **limits),
                        )
                    return origin
                except (OSError, httpx.HTTPError):
                    time.sleep(0.1)
        raise TimeoutError(f"Owned {name} port-forward readiness exceeded its deadline")

    def _start_database_forward(self, member, *, local_port, bind_address="127.0.0.1"):
        if bind_address != "127.0.0.1" or not any(member in group.members for group in self.app.database_groups):
            raise ValueError("SQL upstreams require loopback and an exact declared member")
        region = next(region for region in self.app.regions if region.name == member.region)
        name = member.origin.removeprefix("mysql://").split(".", 1)[0]
        origin = self._start_forward(
            region, name, remote_port=3306, http_ready=False, bind_address=bind_address, local_port=local_port
        )
        parsed = urlsplit(origin)
        return parsed.hostname, parsed.port

    def _prepare_endpoints(self):
        from sregym.generators.workload.codehub_seed import RegionEndpoints

        endpoints = {}
        for region in self.app.regions:
            certificate = self.app.gateway_certificates.get(region.namespace)
            if not certificate:
                raise RuntimeError("Every regional customer gateway requires its real CA")
            ca_file = self._write_private(f"{region.name}-ca.pem", certificate)
            gateway = self._start_forward(region, "gateway", remote_port=8443, ca_file=ca_file)
            # Customer API and Git terminate TLS at the same ordinary gateway.
            topology = self._start_forward(region, "topology")
            self._start_forward(region, "repository")
            for index in range(self.app.tier.search_per_zone):
                self._start_forward(region, f"search-{index}", resource_kind="pod")
            endpoints[region.name] = RegionEndpoints(gateway, gateway, topology, str(ca_file))
        return endpoints

    def prepare_environment(self, *, enable_noise: bool) -> None:
        with self._runtime_lock:
            if self._prepared or self._controller is not None or self._environment_cancel.is_set():
                raise RuntimeError("Regional problems prepare once per deployment")
            if type(enable_noise) is not bool or self.app.inventory.phase != LifecyclePhase.HEALTHY:
                raise RuntimeError("Preparation requires healthy owned deployment and explicit noise policy")
            try:
                self._create_private_directory()
                if isinstance(self.app, CodeHub):
                    from sregym.conductor.scenarios.codehub_capacity import CapacityMonitor, NativeStorageObserver

                    def cancel_capacity():
                        self._environment_cancel.set()
                        if self._controller is not None:
                            self._controller.cancel.set()

                    observer = NativeStorageObserver(self.app, self._private_dir, cancel=self._environment_cancel)
                    self.app._capacity_monitor = CapacityMonitor(
                        observer,
                        self._private_dir / "physical-resources.jsonl",
                        cancel=cancel_capacity,
                        disk_budget=self.app.tier.disk_gib_limit * 1024**3,
                        reserve_gib=self.app.capacity_observation["trusted_reserve_gib"],
                        verifier_reserve_bytes={"small": 1, "medium": 4, "large": 8}[self.app.tier.name] * 1024**3,
                    )
                    self.app._capacity_monitor.start()
                from sregym.conductor.scenarios.codehub_controller import BorrowedOwnerLease, RecoveryController
                from sregym.conductor.scenarios.codehub_observer import DeliveryObserver
                from sregym.generators.workload.codehub import WorkloadClient

                observer_factory = self._observer_factory or DeliveryObserver
                self._observer = observer_factory(
                    self._private_dir / "delivery.sqlite",
                    delivery_address=RUNNER_ADDRESS,
                    delivery_port=DELIVERY_PORT,
                    byte_budget={"small": 256 * 1024**2, "medium": 1024**3, "large": 4 * 1024**3}[self.app.tier.name],
                )
                self._observer.start()
                credentials = self.app._credentials
                if not credentials or not credentials.get("bootstrap-token") or not credentials.get("service-token"):
                    raise RuntimeError("Normal application credentials were not created during deployment")
                self._write_private("bootstrap-token", credentials["bootstrap-token"])
                self._write_private("service-token", credentials["service-token"])
                extra = {}
                if self._link_factory is not None:
                    self._link_binding = self._link_factory(self.app, self._private_dir, self._start_database_forward)
                    if not isinstance(self._link_binding, RegionalLinkBinding):
                        raise TypeError("Optional regional link factory must return an owned RegionalLinkBinding")
                    extra = {
                        "writer_endpoint": self._link_binding.writer_endpoint,
                        "network_factory": self._link_binding.network_factory,
                    }
                    self.has_regional_links = True
                # The link installer may roll out the normal topology controller.
                # HTTP service forwards start afterward so they track current pods.
                endpoints = self._prepare_endpoints()
                ca_contexts = {
                    endpoint.api: ssl.create_default_context(cafile=endpoint.ca_file) for endpoint in endpoints.values()
                }

                def client_factory(origin, token, ledger):
                    return WorkloadClient(origin, token, ledger, verify=ca_contexts.get(origin, True))

                controller_factory = self._controller_factory or RecoveryController
                if getattr(self.app, "_environment_lease", None) is not None:
                    extra["lease"] = BorrowedOwnerLease(self.app._environment_lease)
                self._controller = controller_factory(
                    self.app,
                    self._private_dir,
                    endpoints,
                    bootstrap_token=credentials["bootstrap-token"],
                    service_token=credentials["service-token"],
                    delivery_url=self._observer.delivery_url,
                    lease_path=self.lease_path or self._private_dir.parent / "campaign.lock",
                    seed=self.seed,
                    client_factory=client_factory,
                    **extra,
                )
                if self._environment_cancel.is_set():
                    if hasattr(self._controller, "cancel"):
                        self._controller.cancel.set()
                    raise RuntimeError("Owned preparation cancelled")
                self._controller.prepare(enable_noise=enable_noise, noise_horizon_seconds=self.noise_horizon_seconds)
                if not self._controller.seed_result or not self._controller.seed_result.tenants:
                    raise RuntimeError("Preparation did not record a complete legitimate customer seed")
                seed_result = self._controller.seed_result
                expected_regions = {region.name for region in self.app.regions}
                counts = Counter(tenant.region for tenant in seed_result.tenants)
                if (
                    set(counts) != expected_regions
                    or any(count != self.app.tier.tenants_per_zone for count in counts.values())
                    or {tenant.group for tenant in seed_result.tenants}
                    != {group.name for group in self.app.database_groups}
                    or type(seed_result.accepted_operations) is not int
                    or seed_result.accepted_operations < self.app.tier.records
                ):
                    raise RuntimeError(
                        "The customer seed does not cover the declared regions, groups and accepted record count"
                    )
                self._seed_accepted_operations = seed_result.accepted_operations
                self._prepared = True
            except BaseException as exc:
                try:
                    self.stop_environment()
                except Exception as cleanup_error:
                    exc.add_note(f"Task-owned partial cleanup also failed: {cleanup_error}")
                raise

    def _build_target_inventory(self):
        from sregym.conductor.oracles.regional_database_recovery import HTTPServiceTarget, SQLTarget
        from sregym.conductor.scenarios.codehub_expectations import (
            DatabaseMemberRequirement,
            RegionalTargetInventory,
            RegionReplicaRequirement,
        )

        namespaces = {region.name: region.namespace for region in self.app.regions}
        databases, requirements = [], []
        for group in self.app.database_groups:
            for member in group.members:
                origin = urlsplit(member.origin)
                service = origin.hostname.split(".", 1)[0]
                namespace = namespaces[member.region]
                databases.append(
                    SQLTarget(
                        group.name, member.region, namespace, service, "codehub", "observer", self.app.observer_password
                    )
                )
                requirements.append(DatabaseMemberRequirement(group.name, member.region, namespace, service))
        api, search, repositories, regions, gateways = [], [], [], [], []
        for region in self.app.regions:
            api.append(
                HTTPServiceTarget(
                    region.name,
                    region.namespace,
                    "api",
                    resource_kind="deployment",
                    expected_replicas=self.app.tier.api_per_zone,
                )
            )
            search.append(
                HTTPServiceTarget(
                    region.name,
                    region.namespace,
                    "search",
                    resource_kind="statefulset",
                    expected_replicas=self.app.tier.search_per_zone,
                )
            )
            repositories.append(
                HTTPServiceTarget(
                    region.name, region.namespace, "repository", resource_kind="statefulset", expected_replicas=1
                )
            )
            gateways.append(
                HTTPServiceTarget(
                    region.name,
                    region.namespace,
                    "gateway",
                    port=8443,
                    resource_kind="deployment",
                    expected_replicas=2,
                    scheme="https",
                    ca_certificate=self.app.gateway_certificates[region.namespace],
                )
            )
            regions.append(
                RegionReplicaRequirement(
                    region.name, region.namespace, self.app.tier.api_per_zone, self.app.tier.search_per_zone, 1
                )
            )
        return RegionalTargetInventory(
            tuple(databases),
            tuple(api),
            tuple(search),
            tuple(repositories),
            tuple(requirements),
            tuple(regions),
            tuple(gateways),
        )

    def _snapshot(self, *, first: bool):
        if self._observer is not None and getattr(self._observer, "capacity_error", None) is not None:
            from sregym.service.verifier_runtime import VerifierError

            raise VerifierError("Trusted delivery receiver exceeded its storage budget or became unavailable")
        for forward in self._forwards:
            if forward.supervisor_error is not None:
                raise RuntimeError(
                    "Owned forwarding infrastructure lost its captured resource"
                ) from forward.supervisor_error
        if not self._prepared or self._controller is None:
            raise RuntimeError("Prepared customer evidence is required")
        if not self._controller.resolve_pending(deadline_seconds=30):
            raise RuntimeError("Customer request outcomes remain unresolved")

        def build(inputs, ledger):
            from sregym.conductor.oracles.regional_database_recovery import (
                DeliveryObserverTarget,
                ProcessTarget,
                WebhookDestination,
            )
            from sregym.conductor.scenarios.codehub_expectations import (
                WebhookSubscription,
                build_receipt_cuts,
                build_recovery_outcomes,
                compile_seed_git_inventory,
            )
            from sregym.generators.workload.codehub_seed import WEBHOOK_EVENTS
            from sregym.service.codehub_verification_journal import CodeHubVerificationJournal

            routing = tuple(sorted((tenant.tenant_id, tenant.group) for tenant in inputs.tenants))
            git_entries = ledger.git_provenance_entries()
            if first:
                seed_projects = compile_seed_git_inventory(inputs.tenants, git_entries)
                inventory = self._build_target_inventory()
                webhooks = tuple(
                    WebhookSubscription(
                        tenant.tenant_id,
                        WebhookDestination(
                            tenant.webhook_id, self._observer.delivery_url, tuple(sorted(WEBHOOK_EVENTS))
                        ),
                    )
                    for tenant in inputs.tenants
                )
            else:
                if routing != self._routing:
                    raise RuntimeError("A verification attempt cannot replace frozen tenant routing")
                seed_projects, inventory, webhooks = self._seed_projects, self._target_inventory, self._webhooks
            cuts, effects = build_receipt_cuts(ledger, routing)
            if sum(cut.operations for cut in cuts) < self._seed_accepted_operations:
                raise RuntimeError("Closed acknowledged evidence omits original seeded operations")
            outcomes = build_recovery_outcomes(
                inputs.tenants,
                routing,
                seed_projects=seed_projects,
                git_entries=git_entries,
                inventory=inventory,
                observer=DeliveryObserverTarget("", "", transport="private_pipe"),
                service_token=self.app._credentials["service-token"],
                webhooks=webhooks,
            )
            required_processes = []
            for region in self.app.regions:
                for kind, name, count in (
                    ("Deployment", "worker", self.app.tier.workers_per_zone),
                    ("StatefulSet", "queue", 3),
                    ("StatefulSet", "delivery", 1),
                    ("Deployment", "topology", 1),
                ):
                    resource = next(
                        (
                            item
                            for item in self.app.inventory.resources
                            if (item.kind, item.namespace, item.name) == (kind, region.namespace, name)
                        ),
                        None,
                    )
                    if resource is None:
                        raise RuntimeError("Captured process inventory omits required regional work")
                    required_processes.append(
                        ProcessTarget(region.name, region.namespace, name, kind.lower(), count, resource.uid)
                    )
            outcomes = replace(outcomes, process_targets=tuple(required_processes), require_traffic=True)
            if first:
                journal_factory = self._journal_factory or CodeHubVerificationJournal
                self._fresh_journal = journal_factory(self._controller.ledger, dict(routing), observer=self._observer)
                if type(self._fresh_journal) is CodeHubVerificationJournal:
                    self._fresh_journal.traffic_source = getattr(self._controller, "traffic_facts", None)
            return routing, seed_projects, inventory, webhooks, cuts, effects, outcomes

        return self._controller.verification_snapshot(build)

    def capture_baseline(self) -> None:
        with self._runtime_lock:
            if self._baseline_captured:
                raise RuntimeError("Regional recovery expectations can be captured only once")
            routing, seed_projects, inventory, webhooks, cuts, effects, outcomes = self._snapshot(first=True)
            oracle = self.mitigation_oracle
            oracle.databases, oracle.cuts, oracle.effect_cuts, oracle.outcomes = (
                inventory.databases,
                cuts,
                effects,
                outcomes,
            )
            oracle.fresh_journal = self._fresh_journal
            RegionalDatabaseRecoveryOracle.capture_baseline(oracle)
            self._routing, self._seed_projects = routing, seed_projects
            self._target_inventory, self._webhooks = inventory, webhooks
            if isinstance(self.app, CodeHub):
                self._capture_dataset_manifest(cuts)
            self._baseline_captured = True

    def _capture_dataset_manifest(self, cuts):
        from sregym.conductor.scenarios.codehub_expectations import build_dataset_manifest
        from sregym.conductor.scenarios.codehub_reference_repair import DatabaseReferenceRepair

        capacity = self.app._capacity_monitor
        capacity.assert_available()
        physical = capacity.observe()
        queue_count, oldest = 0, 0
        reader = DatabaseReferenceRepair(self.app, self._private_dir, cancel=self._environment_cancel)
        reader._deadline = time.monotonic() + 30
        for group in self.app.database_groups:
            writer = next(member for member in group.members if member.role == "writer")
            rows = reader._sql_json(
                writer,
                "SELECT JSON_OBJECT('count',COUNT(*),'oldest',COALESCE(MAX(GREATEST(0,TIMESTAMPDIFF(SECOND,j.accepted_at,UTC_TIMESTAMP(6)))),0)) "
                "FROM codehub.outbox o JOIN codehub.journal j USING(event_id) WHERE o.state<>'done';",
            )
            if len(rows) != 1:
                raise RuntimeError("Normal durable work backlog observation is unavailable")
            facts = rows[0]
            if set(facts) != {"count", "oldest"} or any(
                type(value) is not int or value < 0 for value in facts.values()
            ):
                raise RuntimeError("Normal durable work backlog observation is unavailable")
            queue_count += facts["count"]
            oldest = max(oldest, facts["oldest"])

        manifest = self._controller.verification_snapshot(
            lambda inputs, ledger: build_dataset_manifest(
                ledger,
                inputs.tenants,
                cuts,
                self.app.database_groups,
                physical,
                queue_count=queue_count,
                oldest_queue_age_seconds=oldest,
            )
        )
        payload = {
            "run_id": self.app.inventory.run_id,
            "manifest": asdict(manifest),
            "queue_scope": "Outstanding durable business effects across writers, including pending and published jobs",
            "recovery_source_scope": "Protected full-payload journal histories; source containment is checked by isolated healthy verification",
            "physical_observation": physical,
            "sampled_peak_memory_bytes": capacity.peak_memory_bytes,
        }
        self._write_private("dataset-manifest.json", json.dumps(payload, sort_keys=True))

    def prepare_verification(self) -> None:
        with self._runtime_lock:
            if not self._baseline_captured:
                raise RuntimeError("Capture the original healthy baseline before verification")
            _, _, _, _, cuts, effects, outcomes = self._snapshot(first=False)
            self.mitigation_oracle.install_verification_snapshot(cuts=cuts, effect_cuts=effects, outcomes=outcomes)

    def inject_fault(self):
        with self._runtime_lock:
            if not self._baseline_captured or self._controller is None:
                raise RuntimeError("Fault injection requires complete original baseline evidence")
            evidence = self._controller.inject_fault()
            self.fault_injected = True
            return evidence

    def recover_fault(self):
        with self._runtime_lock:
            if not self.fault_injected or self._controller is None:
                raise RuntimeError("Normal reference recovery requires an injected owned incident")
            result = self._controller.reference_repair(timeout=self.reference_repair_seconds)
            self.fault_injected = False
            return result

    def stop_environment(self) -> None:
        """Stop captured private services only; application resources belong to app.cleanup."""
        self._environment_cancel.set()
        controller = self._controller
        if controller is not None and hasattr(controller, "cancel"):
            controller.cancel.set()
        with self._runtime_lock:
            failures = []
            if self._controller is not None:
                try:
                    self._controller.stop(timeout=30)
                    noise = getattr(self._controller, "noise", None)
                    if noise is not None:
                        try:
                            noise.assert_completed()
                        except Exception:
                            self.environment_failure = "real_noise_owner_failed"
                    self._controller = None
                except Exception as exc:
                    failures.append(exc)
            # Preserve receiver/forward availability if traffic still failed to stop.
            if self._controller is None:
                capacity = getattr(self.app, "_capacity_monitor", None)
                if capacity is not None:
                    try:
                        capacity.stop()
                        if capacity.error is not None:
                            self.environment_failure = "native_capacity_observation_failed"
                        self.app._capacity_monitor = None
                    except Exception as exc:
                        failures.append(exc)
                if self._link_binding is not None:
                    try:
                        self._link_binding.owner.stop()
                        self._link_binding = None
                    except Exception as exc:
                        failures.append(exc)
                for forward in reversed(self._forwards):
                    try:
                        forward.close()
                    except Exception as exc:
                        failures.append(exc)
                self._forwards = [forward for forward in self._forwards if not forward.closed]
                if self._observer is not None:
                    try:
                        self._observer.close()
                        self._observer = None
                    except Exception as exc:
                        failures.append(exc)
                self._prepared = False
            if failures:
                raise ExceptionGroup("Task-owned regional cleanup is incomplete", failures)
