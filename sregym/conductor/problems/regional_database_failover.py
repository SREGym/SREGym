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
from dataclasses import dataclass
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

    def close(self):
        if self.closed:
            return
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        self.diagnostics.close()
        self.closed = True


class _RecoveryOracle(RegionalDatabaseRecoveryOracle):
    def capture_baseline(self):
        self.problem.capture_baseline()


class RegionalDatabaseFailover(Problem):
    run_default_workload = False
    run_default_noise = False
    verifier_excluded_fields = (
        "_controller",
        "_observer",
        "_forwards",
        "_link_binding",
        "_runtime_lock",
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

    def _assert_namespace(self, namespace):
        resource = next(
            (r for r in self.app.inventory.resources if r.kind == "Namespace" and r.name == namespace), None
        )
        if resource is None:
            raise RuntimeError("Forward namespace is absent from the owned inventory")
        current = self.app._client().core_v1_api.read_namespace(namespace, _request_timeout=5)
        if (
            current.metadata.uid != resource.uid
            or (current.metadata.labels or {}).get("codehub.local/deployment") != self.app.deployment_owner
        ):
            raise RuntimeError("Forward namespace ownership changed")

    def _assert_forward_resource(self, region, resource_kind, name):
        self._assert_namespace(region.namespace)
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
            pod = core.read_namespaced_pod(name, region.namespace, _request_timeout=5)
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
        current = core.read_namespaced_service(name, region.namespace, _request_timeout=5)
        if resource is None or current.metadata.uid != resource.uid:
            raise RuntimeError("Forward service ownership changed")

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
        try:
            process = self._popen_factory(
                [
                    "kubectl",
                    "--request-timeout=10s",
                    "-n",
                    region.namespace,
                    "port-forward",
                    f"{resource_kind}/{name}",
                    f"{port}:{remote_port}",
                    "--address",
                    bind_address,
                ],
                stdin=subprocess.DEVNULL,
                stdout=diagnostics,
                stderr=diagnostics,
            )
        except BaseException:
            diagnostics.close()
            raise
        origin = f"{'https' if ca_file else 'http'}://{bind_address}:{port}"
        self._forwards.append(_Forward(process, diagnostics, origin))
        deadline = time.monotonic() + self.readiness_seconds
        context = ssl.create_default_context(cafile=str(ca_file)) if ca_file is not None else True
        with httpx.Client(verify=context, timeout=2, follow_redirects=False, trust_env=False) as client:
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError(f"Owned {name} port-forward exited before readiness")
                try:
                    if http_ready:
                        response = client.get(origin + "/readyz")
                        response.raise_for_status()
                    else:
                        with socket.create_connection((bind_address, port), timeout=0.2):
                            pass
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
            if self._prepared or self._controller is not None:
                raise RuntimeError("Regional problems prepare once per deployment")
            if type(enable_noise) is not bool or self.app.inventory.phase != LifecyclePhase.HEALTHY:
                raise RuntimeError("Preparation requires healthy owned deployment and explicit noise policy")
            try:
                self._create_private_directory()
                from sregym.conductor.scenarios.codehub_controller import RecoveryController
                from sregym.conductor.scenarios.codehub_observer import DeliveryObserver
                from sregym.generators.workload.codehub import WorkloadClient

                observer_factory = self._observer_factory or DeliveryObserver
                self._observer = observer_factory(
                    self._private_dir / "delivery.sqlite", delivery_address=RUNNER_ADDRESS, delivery_port=DELIVERY_PORT
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
                self._controller.prepare(enable_noise=enable_noise)
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
        api, search, repositories, regions = [], [], [], []
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
            regions.append(
                RegionReplicaRequirement(
                    region.name, region.namespace, self.app.tier.api_per_zone, self.app.tier.search_per_zone, 1
                )
            )
        return RegionalTargetInventory(
            tuple(databases), tuple(api), tuple(search), tuple(repositories), tuple(requirements), tuple(regions)
        )

    def _snapshot(self, *, first: bool):
        if not self._prepared or self._controller is None:
            raise RuntimeError("Prepared customer evidence is required")
        if not self._controller.resolve_pending(deadline_seconds=30):
            raise RuntimeError("Customer request outcomes remain unresolved")

        def build(inputs, ledger):
            from sregym.conductor.oracles.regional_database_recovery import DeliveryObserverTarget, WebhookDestination
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
            if first:
                journal_factory = self._journal_factory or CodeHubVerificationJournal
                self._fresh_journal = journal_factory(ledger, dict(routing), observer=self._observer)
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
            self._baseline_captured = True

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
            result = self._controller.reference_repair()
            self.fault_injected = False
            return result

    def stop_environment(self) -> None:
        """Stop captured private services only; application resources belong to app.cleanup."""
        with self._runtime_lock:
            failures = []
            if self._controller is not None:
                try:
                    self._controller.stop(timeout=30)
                    self._controller = None
                except Exception as exc:
                    failures.append(exc)
            # Preserve receiver/forward availability if traffic still failed to stop.
            if self._controller is None:
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
