"""Private customer seeding through authenticated API and actual Git journeys."""

import base64
import hashlib
import io
import json
import logging
import os
import random
import secrets
import ssl
import subprocess
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from queue import Empty, Full, Queue
from tempfile import TemporaryDirectory
from threading import Event, Lock, Thread
from uuid import uuid4

import httpx

from sregym.generators.workload.codehub import Operation, WorkloadClient, canonical
from sregym.generators.workload.http_deadline import DeadlineTransport


@dataclass(frozen=True)
class RegionEndpoints:
    api: str
    repository: str
    topology: str
    ca_file: str | None = None


@dataclass(frozen=True)
class TenantAccount:
    tenant_id: str
    project_id: str
    region: str
    group: str
    owner_id: str
    owner_token: str = field(repr=False)
    reviewer_id: str = ""
    reviewer_token: str = field(default="", repr=False)
    webhook_id: str = ""
    git_commit: str = ""
    git_files: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class SeedResult:
    tenants: tuple[TenantAccount, ...]
    accepted_operations: int
    file_hashes: tuple[tuple[str, str, str], ...]


WEBHOOK_EVENTS = frozenset(
    {"issue.create", "issue.update", "comment.create", "repository.push", "change.create", "change.update"}
)


def expected_effects(operation, subscriptions=()):
    """Derive requested effects from private client intent, never database rows."""
    request = operation.request()
    effects = []

    def append(kind, destination, payload):
        effects.append(
            {
                "effect_id": hashlib.sha256(f"{operation.event_id}/{kind}/{destination}".encode()).hexdigest(),
                "event_id": operation.event_id,
                "effect_kind": kind,
                "destination": destination,
                "payload": payload,
            }
        )

    if operation.kind.split(".", 1)[0] in {"project", "issue", "comment", "change", "review", "repository"}:
        append("search", operation.entity_id, request)
    if operation.kind in {"repository.push", "change.create", "change.update"}:
        commit = request["payload"].get("commit_sha") or request["payload"].get("head_sha")
        if commit:
            append("build", operation.project_id, request | {"commit_sha": commit})
    for subscription in subscriptions:
        if subscription["enabled"] and operation.kind in subscription["events"]:
            append("delivery", subscription["id"], {"operation": request, "url": subscription["url"]})
    return tuple(effects)


def account_subscriptions(account, delivery_url):
    return ({"id": account.webhook_id, "events": WEBHOOK_EVENTS, "url": delivery_url, "enabled": True},)


def source_provenance(root, project_id, commit, ref):
    """Capture source bytes and deterministic artifact expectations before upload."""
    files = {name: (root / name).read_bytes() for name in ("README.md", "routing.py")}
    checksums = {name: hashlib.sha256(content).hexdigest() for name, content in sorted(files.items())}
    manifest = {
        "project_id": project_id,
        "commit_sha": commit,
        "builder": "python-validate-bundle-v1",
        "files": checksums,
    }
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for name, content in [*sorted(files.items()), ("CODEHUB-BUILD.json", canonical(manifest).encode())]:
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.external_attr = 0o644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            bundle.writestr(info, content)
    return {
        "project_id": project_id,
        "refs": [{"ref": ref, "commit_sha": commit}],
        "files": [{"commit_sha": commit, "path": name, "sha256": checksum} for name, checksum in checksums.items()],
        "bundles": [{"commit_sha": commit, "sha256": hashlib.sha256(output.getvalue()).hexdigest()}],
    }


class CustomerSeeder:
    WORKER_STOP_SECONDS = 35

    def __init__(
        self,
        *,
        endpoints,
        ledger,
        bootstrap_token,
        delivery_url,
        seed,
        client_factory=WorkloadClient,
        route_tenant=None,
        routes_ready=None,
        fill_workers=1,
        cancel=None,
        deadline_seconds=86400,
    ):
        if type(fill_workers) is not int or not 1 <= fill_workers <= 64:
            raise ValueError("Bulk customer history requires 1..64 workers")
        self.endpoints = endpoints
        self.ledger = ledger
        self.bootstrap_token = bootstrap_token
        self.delivery_url = delivery_url
        self.random = random.Random(seed)
        self.client_factory = client_factory
        self.accepted = 0
        self._accepted_lock = Lock()
        self.fill_workers = fill_workers
        self.route_tenant = route_tenant
        self.routes_ready = routes_ready
        self.subscriptions = {}
        self.git_cas = {}
        if type(deadline_seconds) is not int or not 1 <= deadline_seconds <= 86400:
            raise ValueError("Customer preparation requires a bounded deadline of at most one day")
        self.cancel = cancel if cancel is not None else Event()
        self.deadline = time.monotonic() + deadline_seconds
        self._started = time.monotonic()
        self._workers, self._fill_clients = [], []

    def drain(self, timeout=35, *, cancel=True):
        """Keep clients and evidence owned until every preparation worker exits."""
        if cancel:
            self.cancel.set()
        deadline = time.monotonic() + max(0, timeout)
        for thread in self._workers:
            thread.join(timeout=max(0, deadline - time.monotonic()))
        if any(thread.is_alive() for thread in self._workers):
            raise RuntimeError("Customer preparation workers did not stop within their shared deadline")
        self._workers.clear()
        while self._fill_clients:
            self._fill_clients[-1].close()
            self._fill_clients.pop()

    def _check_active(self):
        if self.cancel.is_set():
            raise RuntimeError("Customer preparation cancelled")
        if time.monotonic() >= self.deadline:
            self.cancel.set()
            raise TimeoutError("Customer preparation deadline exceeded")

    def client(self, origin, token):
        self._check_active()
        client = self.client_factory(origin, token, self.ledger)
        if isinstance(client, WorkloadClient):
            client.cancel, client.deadline = self.cancel, self.deadline
        return client

    def provision_user(self, endpoints, user_id, username, token):
        self._check_active()
        verify = ssl.create_default_context(cafile=endpoints.ca_file) if endpoints.ca_file else True
        with httpx.Client(
            base_url=endpoints.api,
            timeout=10,
            verify=verify,
            transport=DeadlineTransport(verify=verify, cancelled=self.cancel.is_set),
        ) as client:
            deadline = min(self.deadline, time.monotonic() + 10)
            content = bytearray()
            with client.stream(
                "POST",
                "/v1/identity/users",
                headers={"Authorization": f"Bearer {self.bootstrap_token}", "Accept-Encoding": "identity"},
                json={"user_id": user_id, "username": username, "api_token": token},
                timeout=max(0.01, deadline - time.monotonic()),
                extensions={"absolute_deadline": deadline},
            ) as response:
                response.raise_for_status()
                if response.headers.get("Content-Encoding", "identity") != "identity":
                    raise RuntimeError("Identity response uses an unsupported content encoding")
                for block in response.iter_bytes():
                    self._check_active()
                    if time.monotonic() >= deadline or len(content) + len(block) > 4096:
                        raise RuntimeError("Identity response exceeded its bounded preparation capacity")
                    content.extend(block)
            if time.monotonic() >= deadline or json.loads(content).get("id") != user_id:
                raise RuntimeError("Identity provisioning returned a different user")

    @staticmethod
    def operation(account, entity_id, revision, kind, payload, *, actor=None, project=True):
        return Operation(
            str(uuid4()),
            account.tenant_id,
            entity_id,
            account.project_id if project else None,
            revision,
            kind,
            canonical(payload),
            actor or account.owner_id,
        )

    def submit(self, client, operation, *, epoch, provenance=None):
        self._check_active()
        effects = expected_effects(operation, self.subscriptions.get((operation.tenant_id, operation.project_id), ()))
        if not client.submit(operation, epoch=epoch, effects=effects, provenance=provenance):
            rejection = getattr(client, "last_rejection", None)
            raise RuntimeError(f"A customer seed operation was not acknowledged: {operation.kind}; {rejection}")
        with self._accepted_lock:
            self.accepted += 1
            if self.accepted % 10000 == 0:
                logging.info(
                    "Private customer preparation: acknowledged=%d seconds=%.3f workers=%d",
                    self.accepted,
                    time.monotonic() - self._started,
                    self.fill_workers,
                )
        if operation.kind == "webhook.create":
            payload = operation.request()["payload"]
            self.subscriptions.setdefault((operation.tenant_id, operation.project_id), []).append(
                {"id": operation.entity_id, **payload}
            )

    def push(self, root, account, client, entity, revision, branch, *, epoch):
        commit = self.git(root, account.owner_token, "rev-parse", "HEAD")
        operation = self.operation(
            account, entity, revision, "repository.push", {"ref": f"refs/heads/{branch}", "commit_sha": commit}
        )
        provenance = source_provenance(root, account.project_id, commit, f"refs/heads/{branch}")
        effects = expected_effects(operation, self.subscriptions[(account.tenant_id, account.project_id)])
        # Persist content provenance before the external Git upload can alter a service.
        self.ledger.request(operation, epoch, effects=effects, provenance=provenance)
        self.git(root, account.owner_token, "push", "origin", branch)
        self.submit(client, operation, epoch=epoch, provenance=provenance)
        return commit

    def git(self, directory, token, *arguments):
        authorization = base64.b64encode(f"customer:{token}".encode()).decode()
        environment = {
            **os.environ,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "http.extraHeader",
            "GIT_CONFIG_VALUE_0": f"Authorization: Basic {authorization}",
        }
        if token in self.git_cas:
            environment["GIT_SSL_CAINFO"] = self.git_cas[token]
        result = subprocess.run(
            ["git", "-c", "commit.gpgsign=false", "-C", str(directory), *arguments],
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=30,
        )
        if result.returncode:
            raise RuntimeError(f"Customer Git operation {arguments[0]} failed")
        return result.stdout.decode().strip()

    def repository_journey(self, account, owner, reviewer, *, epoch):
        from dataclasses import replace

        fixtures = Path(__file__).with_name("codehub_fixtures")
        if self.endpoints[account.region].ca_file:
            self.git_cas[account.owner_token] = self.endpoints[account.region].ca_file
        with TemporaryDirectory(prefix="customer-project-") as directory:
            root = Path(directory)
            for source in fixtures.iterdir():
                if source.is_file() and source.name in {"routing.py", "README.md"}:
                    (root / source.name).write_bytes(source.read_bytes())
            self.git(root, account.owner_token, "init", "-b", "main")
            self.git(root, account.owner_token, "config", "core.autocrlf", "false")
            self.git(root, account.owner_token, "config", "user.name", "Morgan Lee")
            self.git(root, account.owner_token, "config", "user.email", "morgan@harbor.example")
            self.git(root, account.owner_token, "add", "routing.py", "README.md")
            self.git(root, account.owner_token, "commit", "-m", "Introduce deterministic gateway routing")
            repository = f"{self.endpoints[account.region].repository.rstrip('/')}/git/{account.project_id}"
            self.git(root, account.owner_token, "remote", "add", "origin", repository)
            main_identity = str(uuid4())
            base = self.push(root, account, owner, main_identity, 1, "main", epoch=epoch)
            self.git(root, account.owner_token, "checkout", "-b", "empty-inventory")
            source = (root / "routing.py").read_text(encoding="utf-8")
            source = source.replace(
                "return sorted(regions)[0]",
                "if not regions:\n        raise ValueError('No healthy destination')\n    return sorted(regions)[0]",
            )
            (root / "routing.py").write_text(source, encoding="utf-8", newline="\n")
            self.git(root, account.owner_token, "add", "routing.py")
            self.git(root, account.owner_token, "commit", "-m", "Handle empty destination inventory")
            head = self.push(root, account, owner, str(uuid4()), 1, "empty-inventory", epoch=epoch)
            change = str(uuid4())
            details = {
                "title": "Handle empty destination inventory",
                "head_sha": head,
                "base_sha": base,
                "head_ref": "refs/heads/empty-inventory",
                "base_ref": "refs/heads/main",
                "state": "open",
            }
            self.submit(owner, self.operation(account, change, 1, "change.create", details), epoch=epoch)
            self.submit(
                reviewer,
                self.operation(
                    account,
                    str(uuid4()),
                    1,
                    "review.create",
                    {
                        "change_id": change,
                        "head_sha": head,
                        "verdict": "approve",
                        "body": "Empty inventory now has an explicit error",
                    },
                    actor=account.reviewer_id,
                ),
                epoch=epoch,
            )
            self.git(root, account.owner_token, "checkout", "main")
            self.git(root, account.owner_token, "merge", "--ff-only", "empty-inventory")
            self.push(root, account, owner, main_identity, 2, "main", epoch=epoch)
            self.submit(
                owner,
                self.operation(account, change, 2, "change.update", {**details, "base_sha": head, "state": "merged"}),
                epoch=epoch,
            )
            self.git(root, account.owner_token, "fsck", "--full")
            files = tuple(
                (name, hashlib.sha256((root / name).read_bytes()).hexdigest()) for name in ("routing.py", "README.md")
            )
            return replace(account, git_commit=head, git_files=files)

    def seed(self, tier, *, epoch=None):
        epoch = self.ledger.begin_epoch() if epoch is None else epoch
        accounts = []
        names = ("Harbor Engineering", "Orchard Data", "Cedar Systems", "Meadow Analytics")
        structural_operations = tier.regions * tier.tenants_per_zone * 13
        if tier.records < structural_operations:
            raise ValueError("Tier cannot fit complete customer journeys")
        if tier.database_groups > 1 and self.route_tenant is None:
            raise RuntimeError("Additional groups require explicit normal tenant routing before seeding")
        if len(self.endpoints) != tier.regions or tier.regions * tier.tenants_per_zone < tier.database_groups:
            raise ValueError("Every declared region and database group requires actual tenant traffic")
        planned = tuple(
            TenantAccount(
                str(uuid4()),
                str(uuid4()),
                region,
                f"group-{ordinal % tier.database_groups}",
                str(uuid4()),
                secrets.token_urlsafe(32),
                str(uuid4()),
                secrets.token_urlsafe(32),
                str(uuid4()),
            )
            for ordinal, region in enumerate(
                region for region in sorted(self.endpoints) for _index in range(tier.tenants_per_zone)
            )
        )
        # All legitimate tenant routes precede identity copies and the first create.
        if self.route_tenant:
            for account in planned:
                self.route_tenant(account.tenant_id, account.group)
        if self.routes_ready:
            self.routes_ready()
        try:
            for region in sorted(self.endpoints):
                for index in range(tier.tenants_per_zone):
                    account = planned[len(accounts)]
                    endpoint = self.endpoints[region]
                    self.provision_user(
                        endpoint, account.owner_id, f"morgan-{account.owner_id[:8]}", account.owner_token
                    )
                    self.provision_user(
                        endpoint, account.reviewer_id, f"jamie-{account.reviewer_id[:8]}", account.reviewer_token
                    )
                    owner = self.client(endpoint.api, account.owner_token)
                    reviewer = self.client(endpoint.api, account.reviewer_token)
                    try:
                        self.submit(
                            owner,
                            self.operation(
                                account,
                                account.tenant_id,
                                1,
                                "organization.create",
                                {"slug": f"harbor-{account.tenant_id[:8]}", "name": names[index % len(names)]},
                                project=False,
                            ),
                            epoch=epoch,
                        )
                        self.submit(
                            owner,
                            self.operation(
                                account,
                                account.reviewer_id,
                                1,
                                "membership.set",
                                {"user_id": account.reviewer_id, "role": "developer"},
                                project=False,
                            ),
                            epoch=epoch,
                        )
                        self.submit(
                            owner,
                            self.operation(
                                account,
                                account.project_id,
                                1,
                                "project.create",
                                {"slug": "request-router", "name": "Request Router", "default_ref": "refs/heads/main"},
                            ),
                            epoch=epoch,
                        )
                        self.submit(
                            owner,
                            self.operation(
                                account,
                                account.webhook_id,
                                1,
                                "webhook.create",
                                {
                                    "url": self.delivery_url,
                                    "events": sorted(WEBHOOK_EVENTS),
                                    "enabled": True,
                                },
                            ),
                            epoch=epoch,
                        )
                        account = self.repository_journey(account, owner, reviewer, epoch=epoch)
                        issue = str(uuid4())
                        self.submit(
                            owner,
                            self.operation(
                                account,
                                issue,
                                1,
                                "issue.create",
                                {
                                    "title": "Empty routing inventory",
                                    "body": "Raise an explicit error when no healthy region is available",
                                    "state": "open",
                                },
                            ),
                            epoch=epoch,
                        )
                        self.submit(
                            reviewer,
                            self.operation(
                                account,
                                str(uuid4()),
                                1,
                                "comment.create",
                                {
                                    "issue_id": issue,
                                    "body": "The change covers this case and builds from the reviewed commit",
                                },
                                actor=account.reviewer_id,
                            ),
                            epoch=epoch,
                        )
                        self.submit(
                            owner,
                            self.operation(
                                account,
                                issue,
                                2,
                                "issue.update",
                                {
                                    "title": "Empty routing inventory",
                                    "body": "Explicit validation has been merged",
                                    "state": "closed",
                                },
                            ),
                            epoch=epoch,
                        )
                    finally:
                        owner.close()
                        reviewer.close()
                    accounts.append(account)
            self.fill_customer_history(tuple(accounts), tier.records - self.accepted, epoch=epoch)
            self._check_active()
            if self.accepted != tier.records:
                raise RuntimeError("Customer seed did not complete its declared accepted-operation count")
            self.ledger.close_epoch(epoch)
        except Exception:
            # An incomplete seed is a setup failure; no partial data claims are emitted.
            raise
        return SeedResult(
            tuple(accounts),
            self.accepted,
            tuple((account.project_id, name, checksum) for account in accounts for name, checksum in account.git_files),
        )

    def _history_journeys(self, accounts, operation_count):
        """Plan bounded immutable journeys on the owner thread, in tenant order."""
        topics = (
            "Retry jitter",
            "Connection pool reuse",
            "Request deadline",
            "Routing inventory refresh",
            "Cache freshness",
        )
        for journey in range((operation_count + 2) // 3):
            ordinal = journey % len(accounts)
            account = accounts[ordinal]
            topic = self.random.choice(topics)
            entity = str(uuid4())
            payload = {
                "title": f"{topic} for gateway change {journey * 3 + 1}",
                "body": f"Review {topic.lower()} under concurrent destination updates. Preserve bounded request handling and add coverage before rollout.",
                "state": "open",
            }
            operations = [self.operation(account, entity, 1, "issue.create", payload)]
            if journey * 3 + 1 < operation_count:
                operations.append(
                    self.operation(
                        account,
                        str(uuid4()),
                        1,
                        "comment.create",
                        {
                            "issue_id": entity,
                            "body": "The service owner will compare retry timing and request latency before enabling the change",
                        },
                    )
                )
            if journey * 3 + 2 < operation_count:
                operations.append(
                    self.operation(
                        account,
                        entity,
                        2,
                        "issue.update",
                        {
                            **payload,
                            "body": f"Reviewed {topic.lower()}; bounded behavior and operational coverage are documented.",
                            "state": "closed",
                        },
                    )
                )
            yield ordinal, tuple(operations)

    def _fill_independent_tenants(self, accounts, clients, operation_count, epoch):
        worker_count = min(self.fill_workers, len(accounts), (operation_count + 2) // 3)
        queues = [Queue(maxsize=2) for _ in range(worker_count)]
        cancelled, error_lock = Event(), Lock()
        errors = []
        started = []

        def work(queue):
            while True:
                if cancelled.is_set() or self.cancel.is_set():
                    return
                try:
                    journey = queue.get(timeout=0.1)
                except Empty:
                    continue
                try:
                    if journey is None:
                        return
                    client, operations = journey
                    for operation in operations:
                        if cancelled.is_set():
                            break
                        try:
                            self.submit(client, operation, epoch=epoch)
                        except BaseException as exc:
                            with error_lock:
                                errors.append(exc)
                            cancelled.set()
                            self.cancel.set()
                            break
                finally:
                    queue.task_done()

        try:
            for index, queue in enumerate(queues):
                thread = Thread(target=work, args=(queue,), name=f"customer-history-{index}", daemon=False)
                thread.start()
                started.append(thread)
                self._workers.append(thread)
            for ordinal, operations in self._history_journeys(accounts, operation_count):
                queue = queues[ordinal % worker_count]
                # A tenant always uses one lane/client. Only independent tenants
                # overlap; no mutable journey or random stream lives in a worker.
                while not cancelled.is_set():
                    self._check_active()
                    try:
                        queue.put((clients[accounts[ordinal].tenant_id], operations), timeout=0.1)
                        break
                    except Full:
                        continue
                if cancelled.is_set():
                    break
        except BaseException:
            cancelled.set()
            self.cancel.set()
            raise
        finally:
            # All started workers drain cancelled plans and exit before clients
            # close or any seed epoch can close, including partial start failure.
            for queue, thread in zip(queues, started, strict=False):
                while thread.is_alive() and not cancelled.is_set() and not self.cancel.is_set():
                    try:
                        self._check_active()
                        queue.put(None, timeout=0.1)
                        break
                    except Full:
                        continue
                    except (RuntimeError, TimeoutError):
                        cancelled.set()
                        break
            cleanup_deadline = time.monotonic() + self.WORKER_STOP_SECONDS
            for thread in started:
                thread.join(timeout=max(0, cleanup_deadline - time.monotonic()))
            if any(thread.is_alive() for thread in started):
                raise RuntimeError("Customer preparation workers did not stop within their shared deadline")
        if errors:
            raise errors[0]
        self._check_active()

    def fill_customer_history(self, accounts, operation_count, *, epoch):
        accounts = tuple(accounts)
        if (
            type(operation_count) is not int
            or operation_count < 0
            or (operation_count and not accounts)
            or len({account.tenant_id for account in accounts}) != len(accounts)
        ):
            raise ValueError("Bulk customer history requires a nonnegative budget and unique tenants")
        if self._workers or self._fill_clients:
            raise RuntimeError("Previous customer preparation ownership has not drained")
        try:
            clients = {}
            for account in accounts:
                client = self.client(self.endpoints[account.region].api, account.owner_token)
                self._fill_clients.append(client)
                clients[account.tenant_id] = client
            if self.fill_workers == 1:
                for ordinal, operations in self._history_journeys(accounts, operation_count):
                    client = clients[accounts[ordinal].tenant_id]
                    for operation in operations:
                        self.submit(client, operation, epoch=epoch)
            elif operation_count:
                self._fill_independent_tenants(accounts, clients, operation_count, epoch)
        finally:
            # An expired join retains all live workers and their clients. The
            # controller can retry draining without closing their receipt DB.
            self.drain(timeout=0, cancel=False)
