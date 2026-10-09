"""Private owned lifecycle, real customer traffic and immutable receipt cuts."""

import json
import math
import os
import shlex
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from sregym.conductor.scenarios.codehub_contracts import LifecyclePhase
from sregym.conductor.scenarios.codehub_reference_repair import DatabaseReferenceRepair, rewrite_worker_group_route
from sregym.conductor.scenarios.database_recovery import SeedStreams, plan_noise
from sregym.generators.fault.regional_database_failover import RegionalFailoverFault
from sregym.generators.noise.codehub import NoiseExecutor
from sregym.generators.workload.codehub import Operation, ReceiptLedger, WorkloadClient, canonical, operation_from_row
from sregym.generators.workload.codehub_seed import CustomerSeeder, account_subscriptions, expected_effects


class OwnerLease:
    """Exclusive same-host campaign ownership; never break another owner's lock."""

    def __init__(self, path):
        self.path, self.handle = Path(path), None

    def acquire(self):
        if self.handle:
            raise RuntimeError("Campaign lease already held")
        if os.name != "posix":
            raise RuntimeError("Live recovery campaigns require the Linux VM lease")
        import fcntl

        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        handle = self.path.open("a+", encoding="utf-8")
        os.chmod(self.path, 0o600)
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException as exc:
            handle.close()
            raise RuntimeError("Another recovery campaign already owns this VM") from exc
        self.handle = handle

    def close(self):
        if self.handle:
            self.handle.close()
            self.handle = None


@dataclass(frozen=True)
class VerificationInputs:
    receipt_cut: object
    partition_cuts: tuple[tuple[str, object], ...]
    tenants: tuple
    incident: object
    noise_executions: tuple
    expected_effects: tuple[tuple[str, tuple], ...]
    git_provenance_entries: tuple


@dataclass(frozen=True)
class CompactVerificationInputs:
    tenants: tuple
    incident: object
    noise_executions: tuple


class RecoveryController:
    def __init__(
        self,
        app,
        private_dir,
        endpoints,
        *,
        bootstrap_token,
        service_token,
        delivery_url,
        lease_path,
        seed=1,
        ledger=None,
        client_factory=WorkloadClient,
        route_tenant=None,
        network_factory=None,
        writer_endpoint=None,
        lease=None,
        operations_per_second=2.0,
        burst_requests_per_second=10.0,
        epoch_seconds=45,
    ):
        if (
            not math.isfinite(operations_per_second)
            or operations_per_second <= 0
            or epoch_seconds < 1
            or not math.isfinite(burst_requests_per_second)
            or burst_requests_per_second <= 0
        ):
            raise ValueError("Traffic rate and mutation-epoch interval must be positive")
        if set(endpoints) != {region.name for region in app.regions}:
            raise ValueError("Managed endpoints must cover every actual application region")
        self.app, self.private_dir, self.endpoints = app, Path(private_dir), endpoints
        self.private_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.ledger = ledger or ReceiptLedger(self.private_dir / "receipts.sqlite")
        self.client_factory, self.network_factory = client_factory, network_factory
        self._tenant_routes = {}
        self._route_tenant_writer = route_tenant or getattr(app, "configure_tenant_route", None)
        self.route_tenant = self._configure_tenant_route if self._route_tenant_writer is not None else None
        self.bootstrap_token, self.service_token, self.delivery_url = bootstrap_token, service_token, delivery_url
        self.writer_endpoint = writer_endpoint
        if writer_endpoint is not None:
            host, port = writer_endpoint
            import re

            if (
                type(host) is not str
                or not re.fullmatch(r"[a-zA-Z0-9.-]{1,253}", host)
                or type(port) is not int
                or not 1 <= port <= 65535
            ):
                raise ValueError("Initial worker routing requires an ordinary database host and port")
        self.seeds = SeedStreams.derive(seed)
        self.lease = lease or OwnerLease(lease_path)
        self.rate, self.epoch_seconds = operations_per_second, epoch_seconds
        self.burst_rate = burst_requests_per_second
        self.cancel = threading.Event()
        self._thread, self._lock = None, threading.RLock()
        self._threads, self._group_sequence = [], {}
        self._inflight_epochs, self._inflight_groups, self._retrying = {}, {}, set()
        self._epoch, self._sequence = self.ledger.begin_epoch(), 0
        self._epoch_started = time.monotonic()
        self._epoch_events, self._pending, self._clients = {}, {}, {}
        self.seed_result, self.fault, self.noise = None, None, None
        self.workload_error = None
        self._stopped = False

    def prepare(self, *, enable_noise=True, noise_horizon_seconds=7200):
        if self.seed_result is not None or self._stopped:
            raise RuntimeError("Recovery controllers are single-deployment owners")
        if self.app.inventory.phase != LifecyclePhase.HEALTHY:
            raise RuntimeError("Application must deploy and report its real healthy lifecycle first")
        self.lease.acquire()
        try:
            # Declare the independent worker routing before seed/baseline observation.
            first_group = self.app.database_groups[0]
            writer = next(member for member in first_group.members if member.role == "writer")
            seeder = CustomerSeeder(
                endpoints=self.endpoints,
                ledger=self.ledger,
                bootstrap_token=self.bootstrap_token,
                delivery_url=self.delivery_url,
                seed=self.seeds.data,
                client_factory=self.client_factory,
                route_tenant=self.route_tenant,
                routes_ready=lambda: self._routes_ready(writer),
                fill_workers=self._seed_fill_workers(),
            )
            self.seed_result = seeder.seed(self.app.tier)
            account = next(account for account in self.seed_result.tenants if account.group == "group-0")
            network = self.network_factory(self.app, first_group) if self.network_factory else None
            self.fault = RegionalFailoverFault(
                self.app,
                self.endpoints,
                account,
                self.ledger,
                self.private_dir,
                service_token=self.service_token,
                network=network,
                delivery_url=self.delivery_url,
                client_factory=self.client_factory,
            )
            for tenant in self.seed_result.tenants:
                self._clients[tenant.tenant_id] = self.client_factory(
                    self.endpoints[tenant.region].api, tenant.owner_token, self.ledger
                )
            plan = plan_noise(
                self.app.tier, self.seeds.noise, horizon_seconds=noise_horizon_seconds, enabled=enable_noise
            )
            self.noise = NoiseExecutor(plan, self.execute_noise, self.private_dir / "noise.jsonl")
            self.start_workload()
            self.noise.start()
            return self.seed_result
        except BaseException:
            self.stop()
            raise

    def _configure_tenant_route(self, tenant_id, group_name):
        self._route_tenant_writer(tenant_id, group_name)
        self._tenant_routes[tenant_id] = group_name

    def _seed_fill_workers(self):
        """Bound preparation by the deployed API replicas and legitimate tenants."""
        regions = len(self.app.regions)
        return min(16, regions * self.app.tier.api_per_zone * 2, regions * self.app.tier.tenants_per_zone)

    def _routes_ready(self, writer):
        wait = getattr(self.app, "await_database_routes", None)
        if callable(wait):
            wait(dict(self._tenant_routes), timeout_seconds=120)
        elif len(self.app.database_groups) > 1:
            raise RuntimeError("Multiple groups require proof of actual mounted tenant routes before seeding")
        self._prepare_worker_route(writer)

    def _prepare_worker_route(self, writer):
        from urllib.parse import urlsplit

        group = next(group for group in self.app.database_groups if writer in group.members)
        candidate = next(member for member in group.members if member.role == "candidate")
        host, port = self.writer_endpoint or (urlsplit(writer.origin).hostname, urlsplit(writer.origin).port or 3306)
        rewrite_worker_group_route(self.app, candidate.region, group.name, host, port)

    def _assert_namespace(self, namespace):
        result = self.app._client().core_v1_api.read_namespace(namespace, _request_timeout=5)
        if not any(
            resource.kind == "Namespace" and resource.name == namespace and resource.uid == result.metadata.uid
            for resource in self.app.inventory.resources
        ):
            raise RuntimeError("Application namespace ownership changed")

    def _remember(self, operation, epoch, client):
        account = next(account for account in self.seed_result.tenants if account.tenant_id == operation.tenant_id)
        effects = expected_effects(operation, account_subscriptions(account, self.delivery_url))
        with self._lock:
            self._epoch_events.setdefault(epoch, set()).add(operation.event_id)
            self.ledger.request(operation, epoch, effects=effects)
        succeeded = client.submit(operation, epoch=epoch, attempts=1, effects=effects)
        with self._lock:
            if not succeeded and any(row["event_id"] == operation.event_id for row in self.ledger.unresolved()):
                self._pending[operation.event_id] = (operation, epoch, client, effects)
        return succeeded

    def _close_resolved_epochs(self):
        pending = {row["epoch"] for row in self.ledger.pending_requests()}
        for epoch in tuple(self._epoch_events):
            if epoch < self._epoch and epoch not in pending and not self._inflight_epochs.get(epoch, 0):
                self.ledger.close_epoch(epoch)
                del self._epoch_events[epoch]

    def traffic_step(self, *, tenant=None, group_name=None):
        with self._lock:
            if self.cancel.is_set():
                return {"requests": 0, "acknowledged": 0}
            accounts = tuple(
                account for account in self.seed_result.tenants if group_name is None or account.group == group_name
            )
            if not accounts:
                raise ValueError("Customer traffic requires actual tenants in the selected group")
            sequence = self._group_sequence.get(group_name, 0)
            account = tenant or accounts[sequence % len(accounts)]
            self._group_sequence[group_name] = sequence + 1
            groups = {entry.tenant_id: entry.group for entry in self.seed_result.tenants}
            retry = next(
                (
                    (identity, entry)
                    for identity, entry in self._pending.items()
                    if groups[entry[0].tenant_id] == account.group and identity not in self._retrying
                ),
                None,
            )
            if retry:
                self._retrying.add(retry[0])
            if time.monotonic() - self._epoch_started >= self.epoch_seconds:
                self._epoch = self.ledger.begin_epoch()
                self._epoch_started = time.monotonic()
                self._close_resolved_epochs()
            pending_count = sum(groups[entry[0].tenant_id] == account.group for entry in self._pending.values())
            admitted = pending_count + self._inflight_groups.get(account.group, 0) < 32
            epoch = self._epoch
            if admitted:
                self._sequence += 1
                self._inflight_epochs[epoch] = self._inflight_epochs.get(epoch, 0) + 1
                self._inflight_groups[account.group] = self._inflight_groups.get(account.group, 0) + 1
                title = f"Gateway deadline review {self._sequence}"
        requests, acknowledged = 0, 0
        try:
            if retry:
                identity, (operation, retry_epoch, retry_client, effects) = retry
                requests += 1
                success = retry_client.submit(operation, epoch=retry_epoch, attempts=1, effects=effects)
                acknowledged += int(success)
                with self._lock:
                    if success or not any(row["event_id"] == identity for row in self.ledger.unresolved()):
                        self._pending.pop(identity, None)
                    self._retrying.discard(identity)
            if not admitted or self.cancel.is_set():
                return {"requests": requests, "acknowledged": acknowledged, "pending_capacity_group": pending_count}
            entity, client = str(uuid4()), self._clients[account.tenant_id]
            payload = {
                "title": title,
                "body": "Review bounded retry timing and connection reuse before the service rollout",
                "state": "open",
            }
            first = Operation(
                str(uuid4()),
                account.tenant_id,
                entity,
                account.project_id,
                1,
                "issue.create",
                canonical(payload),
                account.owner_id,
            )
            requests += 1
            created = self._remember(first, epoch, client)
            acknowledged += int(created)
            if created and not self.cancel.is_set():
                final = Operation(
                    str(uuid4()),
                    account.tenant_id,
                    entity,
                    account.project_id,
                    2,
                    "issue.update",
                    canonical(
                        {**payload, "state": "closed", "body": "Service owners documented bounded retry behavior"}
                    ),
                    account.owner_id,
                )
                requests += 1
                acknowledged += int(self._remember(final, epoch, client))
            return {"requests": requests, "acknowledged": acknowledged}
        finally:
            with self._lock:
                if retry:
                    self._retrying.discard(retry[0])
                if admitted:
                    self._inflight_epochs[epoch] -= 1
                    self._inflight_groups[account.group] -= 1
                self._close_resolved_epochs()

    def _workload(self, group_name):
        groups = {account.group for account in self.seed_result.tenants}
        group_rate = self.rate / len(groups)
        while not self.cancel.is_set():
            try:
                measurements = self.traffic_step(group_name=group_name)
            except Exception as exc:
                self.workload_error = type(exc).__name__
                self.cancel.set()
                return
            self.cancel.wait(max(1, measurements["requests"]) / group_rate)

    def start_workload(self):
        if self._threads or not self.seed_result:
            raise RuntimeError("Workload requires a seeded, single-use controller")
        self._threads = [
            threading.Thread(target=self._workload, args=(group,), name=f"customer-traffic-{group}", daemon=True)
            for group in sorted({account.group for account in self.seed_result.tenants})
        ]
        self._thread = self._threads[0]
        for thread in self._threads:
            thread.start()

    def inject_fault(self):
        if not self.fault or self.workload_error:
            raise RuntimeError("Real prepared workload is unavailable")
        if self.app.inventory.phase == LifecyclePhase.HEALTHY:
            self.app.inventory = self.app.inventory.transition(LifecyclePhase.BASELINE)
        if self.app.inventory.phase != LifecyclePhase.BASELINE:
            raise RuntimeError("Fault injection requires the captured healthy baseline")
        evidence = self.fault.inject()
        self.app.inventory = self.app.inventory.transition(LifecyclePhase.FAULTED).transition(LifecyclePhase.HANDOFF)
        return evidence

    def verification_inputs(self, *, compact=False):
        with self._lock:
            if not self.seed_result or self.workload_error:
                raise RuntimeError("Cannot verify incomplete or failed customer traffic")
            if self.noise and any(
                receipt.admitted and receipt.status in {"failed", "not-executed"} for receipt in self.noise.receipts
            ):
                raise RuntimeError("A required real-noise action failed or exceeded its execution budget")
            self._close_resolved_epochs()
            if compact:
                return CompactVerificationInputs(
                    self.seed_result.tenants,
                    self.fault.evidence if self.fault else None,
                    self.noise.receipts if self.noise else (),
                )
            tenant_groups = {account.tenant_id: account.group for account in self.seed_result.tenants}
            return VerificationInputs(
                self.ledger.cut(),
                tuple(self.ledger.partition_cuts(tenant_groups).items()),
                self.seed_result.tenants,
                self.fault.evidence if self.fault else None,
                self.noise.receipts if self.noise else (),
                tuple(self.ledger.expected_effects(tenant_groups).items()),
                self.ledger.git_provenance_entries(),
            )

    def verification_snapshot(self, builder):
        """Copy a consistent private snapshot while serving traffic continues."""
        with self._lock, self.ledger._lock:
            return builder(self.verification_inputs(compact=True), self.ledger)

    def resolve_pending(self, *, deadline_seconds=30):
        """Retry exact original unknown outcomes without pausing other group traffic."""
        deadline = time.monotonic() + deadline_seconds
        rows = self.ledger.pending_requests()
        original = {row["operation"]["event_id"] for row in rows}
        for row in rows:
            if time.monotonic() >= deadline:
                break
            operation = operation_from_row(row["operation"])
            client = self._clients.get(operation.tenant_id)
            if client is None:
                raise RuntimeError("Pending traffic has no ordinary authenticated tenant client")
            client.submit(
                operation, epoch=row["epoch"], attempts=1, effects=row["effects"], provenance=row["provenance"]
            )
        remaining = {row["event_id"] for row in self.ledger.unresolved()}
        with self._lock:
            for identity in original - remaining:
                self._pending.pop(identity, None)
        return not (original & remaining)

    def reference_repair(self):
        return DatabaseReferenceRepair(self.app, self.private_dir / "normal-recovery").run()

    def _account(self, target):
        region, index = target.split("/")
        accounts = [account for account in self.seed_result.tenants if account.region == region]
        return accounts[int(index.removeprefix("tenant-"))]

    def execute_noise(self, event, cancel):
        account = self._account(event.target)
        if event.operation == "traffic-burst":
            deadline, requests, acknowledged = time.monotonic() + event.duration_seconds, 0, 0
            while time.monotonic() < deadline and not cancel.is_set():
                measurements = self.traffic_step(tenant=account)
                requests += measurements["requests"]
                acknowledged += measurements["acknowledged"]
                cancel.wait(
                    min(max(0, deadline - time.monotonic()), max(1, measurements["requests"]) / self.burst_rate)
                )
            return {
                "requests": requests,
                "acknowledged": acknowledged,
                "duration_seconds": event.duration_seconds,
                "request_rate_limit": self.burst_rate,
            }
        if event.operation == "worker-recycle":
            return self._recycle_worker(account.region)
        if event.operation == "index-refresh":
            return self._refresh_index(account, event.duration_seconds)
        raise ValueError("Unrecognized real-noise operation")

    def _recycle_worker(self, region_name):
        region = next(region for region in self.app.regions if region.name == region_name)
        self._assert_namespace(region.namespace)
        core = self.app._client().core_v1_api
        from kubernetes.client import AppsV1Api

        apps = AppsV1Api(core.api_client)
        deployment = apps.read_namespaced_deployment("worker", region.namespace, _request_timeout=5)
        if not any(
            resource.kind == "Deployment"
            and resource.name == "worker"
            and resource.namespace == region.namespace
            and resource.uid == deployment.metadata.uid
            for resource in self.app.inventory.resources
        ):
            raise RuntimeError("Worker deployment ownership changed")
        pods = core.list_namespaced_pod(
            region.namespace, label_selector="app.kubernetes.io/component=worker", _request_timeout=5
        ).items
        serving = [
            pod
            for pod in pods
            if any(
                condition.type == "Ready" and condition.status == "True" for condition in (pod.status.conditions or [])
            )
        ]
        if len(serving) < 2:
            raise RuntimeError("Worker recycle requires redundant actual serving workers")
        owned = []
        for pod in serving:
            owners = pod.metadata.owner_references or []
            replica_owner = next((owner for owner in owners if owner.kind == "ReplicaSet" and owner.controller), None)
            if replica_owner:
                replica = apps.read_namespaced_replica_set(replica_owner.name, region.namespace, _request_timeout=5)
                if replica.metadata.uid == replica_owner.uid and any(
                    owner.kind == "Deployment" and owner.controller and owner.uid == deployment.metadata.uid
                    for owner in (replica.metadata.owner_references or [])
                ):
                    owned.append(pod)
        if not owned:
            raise RuntimeError("No serving worker belongs to the captured deployment")
        pod = owned[0]
        core.delete_namespaced_pod(
            pod.metadata.name, region.namespace, body={"preconditions": {"uid": pod.metadata.uid}}, _request_timeout=5
        )
        return {
            "deleted_uid": pod.metadata.uid,
            "serving_workers_before": len(serving),
            "remaining_owned_workers": len(owned) - 1,
        }

    def _refresh_index(self, account, duration):
        region = next(region for region in self.app.regions if region.name == account.region)
        self._assert_namespace(region.namespace)
        program = """
import hashlib,json,sys,time,httpx
from codehub.config import Settings
from codehub.http_support import service_headers
from codehub.storage.routing import StorageRouter
settings=Settings.load();store=StorageRouter(settings).for_tenant(sys.argv[1]);deadline=time.monotonic()+int(sys.argv[3])
rows=store.events(sys.argv[1],sys.argv[2],limit=100);count=0
with httpx.Client(headers=service_headers(),timeout=2) as client:
    for row in rows:
        if row['kind'].split('.')[0] not in {'project','issue','comment','change','review','repository'}: continue
        operation={key:row[key] for key in ['event_id','tenant_id','entity_id','project_id','client_revision','kind','payload']}
        identity=hashlib.sha256((row['event_id']+'/search/'+row['entity_id']).encode()).hexdigest()
        for endpoint in settings.search_urls:
            if time.monotonic()>=deadline: break
            response=client.post(endpoint+'/internal/index',json={'effect_id':identity,'operation':operation});response.raise_for_status();count+=1
        if time.monotonic()>=deadline: break
print(json.dumps({'indexed_copies':count,'source_events':len(rows)}))
"""
        command = f"kubectl -n {region.namespace} exec deployment/api -- python -c {shlex.quote(program)} {account.tenant_id} {account.owner_id} {duration}"
        output = self.app._client().exec_command_checked(command, timeout=duration + 15)
        result = json.loads(output.strip().splitlines()[-1])
        if result["indexed_copies"] < 1:
            raise RuntimeError("Index refresh did not perform any actual projection work")
        return result

    def stop(self, *, timeout=60):
        if self._stopped:
            return
        self.cancel.set()
        deadline = time.monotonic() + timeout
        errors = []
        if self.noise:
            try:
                self.noise.stop(timeout=max(0, deadline - time.monotonic()))
            except Exception as exc:
                errors.append(exc)
        for thread in self._threads or ([self._thread] if self._thread else []):
            thread.join(timeout=max(0, deadline - time.monotonic()))
            if thread.is_alive():
                errors.append(RuntimeError("Owned customer traffic exceeded its stop deadline"))
        if self.fault:
            try:
                self.fault.stop()
            except Exception as exc:
                errors.append(exc)
        if errors:
            raise RuntimeError("Owned lifecycle cleanup incomplete") from errors[0]
        for client in self._clients.values():
            client.close()
        self.ledger.close()
        self.lease.close()
        self._stopped = True
