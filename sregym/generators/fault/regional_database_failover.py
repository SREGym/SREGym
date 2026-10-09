"""Private causal regional partition, retained divergent writes and restored handoff."""

import copy
import json
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

from sregym.generators.workload.codehub import Operation, WorkloadClient, canonical
from sregym.generators.workload.codehub_seed import account_subscriptions, expected_effects
from sregym.generators.workload.http_deadline import DeadlineTransport


def service_name(member):
    return urlsplit(member.origin).hostname.split(".", 1)[0]


def sql_json(app, member, query):
    output = app.mysql_command(member, query)
    rows = []
    for line in output.splitlines():
        if line.startswith("{"):
            rows.append(json.loads(line))
    return rows


@dataclass(frozen=True)
class DivergenceEvidence:
    group: str
    issue_id: str
    original_event: str
    original_suffix_event: str
    promoted_suffix_event: str
    old_writer_retains_suffix: bool
    new_writer_retains_suffix: bool
    promoted_at_ns: int
    connectivity_restored_at_ns: int
    pending_jobs: int
    regional_latency_qualified: bool = False


class ScopedDatabasePartition:
    """Replace an owned allow-policy; additional policies cannot subtract allowance."""

    def __init__(self, app, writer, *, networking=None):
        self.app, self.writer = app, writer
        self.region = next(region for region in app.regions if region.name == writer.region)
        if networking is None:
            from kubernetes.client import NetworkingV1Api

            networking = NetworkingV1Api(app._client().core_v1_api.api_client)
        self.api = networking
        self.original = None
        self.temporary_uid = None
        self.name = "database-access-" + service_name(writer)

    def _assert_owner(self, original):
        uid = original.metadata.uid
        if not any(
            resource.kind == "NetworkPolicy"
            and resource.namespace == self.region.namespace
            and resource.name == "regional-services"
            and resource.uid == uid
            for resource in self.app.inventory.resources
        ):
            raise RuntimeError("Network policy is outside captured application ownership")
        namespace = self.app._client().core_v1_api.read_namespace(self.region.namespace, _request_timeout=5)
        if not any(
            resource.kind == "Namespace"
            and resource.name == self.region.namespace
            and resource.uid == namespace.metadata.uid
            for resource in self.app.inventory.resources
        ):
            raise RuntimeError("Namespace ownership changed")

    def apply(self):
        if self.original is not None:
            raise RuntimeError("Partition cannot be applied twice")
        original = self.api.read_namespaced_network_policy(
            "regional-services", self.region.namespace, _request_timeout=5
        )
        self._assert_owner(original)
        self.original = original
        from kubernetes.client import ApiClient

        spec = ApiClient().sanitize_for_serialization(original.spec)
        target_spec = copy.deepcopy(spec)
        target_spec["podSelector"] = {
            "matchLabels": {"app.kubernetes.io/name": "codehub", "database-member": service_name(self.writer)}
        }
        for rule in target_spec.get("ingress", []):
            for peer in rule.get("from", []):
                if (
                    "namespaceSelector" in peer
                    and peer["namespaceSelector"].get("matchLabels", {}).get("app.kubernetes.io/name") == "codehub"
                ):
                    peer["namespaceSelector"] = {"matchLabels": {"kubernetes.io/metadata.name": self.region.namespace}}
        temporary = self.api.create_namespaced_network_policy(
            self.region.namespace,
            body={
                "apiVersion": "networking.k8s.io/v1",
                "kind": "NetworkPolicy",
                "metadata": {"name": self.name, "namespace": self.region.namespace},
                "spec": target_spec,
            },
            _request_timeout=5,
        )
        self.temporary_uid = temporary.metadata.uid
        excluded = copy.deepcopy(spec["podSelector"])
        excluded.setdefault("matchExpressions", []).append(
            {"key": "database-member", "operator": "NotIn", "values": [service_name(self.writer)]}
        )
        try:
            self.api.patch_namespaced_network_policy(
                "regional-services",
                self.region.namespace,
                body={
                    "metadata": {"resourceVersion": original.metadata.resource_version},
                    "spec": {"podSelector": excluded},
                },
                _request_timeout=5,
            )
        except BaseException:
            self.restore()
            raise

    def restore(self):
        if self.original is None:
            return
        from kubernetes.client import ApiClient

        original = self.original
        current = self.api.read_namespaced_network_policy(
            "regional-services", self.region.namespace, _request_timeout=5
        )
        self._assert_owner(current)
        if current.metadata.uid != original.metadata.uid:
            raise RuntimeError("Cannot restore a replacement network policy")
        self.api.patch_namespaced_network_policy(
            "regional-services",
            self.region.namespace,
            body={
                "metadata": {"resourceVersion": current.metadata.resource_version},
                "spec": ApiClient().sanitize_for_serialization(original.spec),
            },
            _request_timeout=5,
        )
        if self.temporary_uid:
            temporary = self.api.read_namespaced_network_policy(self.name, self.region.namespace, _request_timeout=5)
            if temporary.metadata.uid != self.temporary_uid:
                raise RuntimeError("Cannot delete a replacement temporary policy")
            self.api.delete_namespaced_network_policy(
                self.name,
                self.region.namespace,
                body={"preconditions": {"uid": self.temporary_uid}},
                _request_timeout=5,
            )
        self.original, self.temporary_uid = None, None


class RegionalFailoverFault:
    def __init__(
        self,
        app,
        endpoints,
        account,
        ledger,
        private_dir: Path,
        *,
        service_token,
        delivery_url,
        network=None,
        client_factory=WorkloadClient,
        promotion_timeout=60,
        cancel=None,
    ):
        self.app, self.endpoints, self.account, self.ledger = app, endpoints, account, ledger
        self.private_dir, self.service_token = private_dir, service_token
        self.group = next(group for group in app.database_groups if group.name == account.group)
        self.writer = next(member for member in self.group.members if member.role == "writer")
        self.candidate = next(member for member in self.group.members if member.role == "candidate")
        self.network = network or ScopedDatabasePartition(app, self.writer)
        self.client_factory, self.promotion_timeout = client_factory, promotion_timeout
        self.evidence = None
        self.subscriptions = account_subscriptions(account, delivery_url)
        self.cancel = cancel if cancel is not None else threading.Event()
        self._deadline = None

    def _remaining(self, maximum=30):
        if self.cancel.is_set():
            raise RuntimeError("Regional incident preparation cancelled")
        capacity = getattr(self.app, "_capacity_monitor", None)
        if capacity is not None:
            capacity.assert_available()
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Regional incident preparation deadline expired")
        return min(maximum, remaining)

    def _sql(self, member, query):
        self._remaining()
        from sregym.conductor.scenarios.codehub_reference_repair import DatabaseReferenceRepair
        from sregym.service.apps.codehub import CodeHub

        if isinstance(self.app, CodeHub):
            repair = DatabaseReferenceRepair(self.app, self.private_dir, cancel=self.cancel)
            repair._deadline = self._deadline
            return repair._sql(member, query)
        return self.app.mysql_command(member, query)

    def _json(self, member, query):
        return [json.loads(line) for line in self._sql(member, query).splitlines() if line.startswith("{")]

    def prepare_worker_routing(self):
        """Pin only the selected ordinary group before healthy baseline observations."""
        from sregym.conductor.scenarios.codehub_reference_repair import rewrite_worker_group_route

        parsed = urlsplit(self.writer.origin)
        rewrite_worker_group_route(
            self.app, self.candidate.region, self.group.name, parsed.hostname, parsed.port or 3306
        )

    def present(self, member, event_id):
        rows = self._json(
            member,
            f"SELECT JSON_OBJECT('present',COUNT(*)) FROM codehub.journal WHERE event_id='{event_id}';",
        )
        return rows and rows[0]["present"] == 1

    def _wait_present(self, member, event_id, deadline):
        while time.monotonic() < deadline:
            if self.present(member, event_id):
                return
            self.cancel.wait(min(0.25, self._remaining()))
        raise RuntimeError("The real regional replica did not receive the required baseline operation")

    def _operation(self, issue, revision, payload):
        return Operation(
            str(uuid4()),
            self.account.tenant_id,
            issue,
            self.account.project_id,
            revision,
            "issue.create" if revision == 1 else "issue.update",
            canonical(payload),
            self.account.owner_id,
        )

    def inject(self, *, epoch=None, suffix_operations=32):
        if self.evidence is not None or suffix_operations < 1:
            raise ValueError("Incident requires a fresh lifecycle and positive real suffix")
        self._deadline = time.monotonic() + 2 * self.promotion_timeout + 120
        self._remaining()
        self.private_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        epoch = self.ledger.begin_epoch() if epoch is None else epoch
        original = self.client_factory(self.endpoints[self.writer.region].api, self.account.owner_token, self.ledger)
        promoted = self.client_factory(self.endpoints[self.candidate.region].api, self.account.owner_token, self.ledger)
        for client in (original, promoted):
            client.cancel, client.deadline = self.cancel, self._deadline
        issue = str(uuid4())
        base = self._operation(
            issue,
            1,
            {"title": "Gateway retry timing", "body": "Compare retry timing with request deadlines", "state": "open"},
        )
        old = self._operation(
            issue,
            2,
            {
                "title": "Gateway retry timing",
                "body": "Use a bounded retry interval for regional requests",
                "state": "open",
            },
        )
        new = self._operation(
            issue,
            3,
            {
                "title": "Gateway retry timing",
                "body": "Document the reviewed retry interval and deadline",
                "state": "closed",
            },
        )
        restored_at = None
        try:
            self._remaining()
            if not original.submit(base, epoch=epoch, effects=expected_effects(base, self.subscriptions)):
                raise RuntimeError("Baseline request did not commit")
            self._wait_present(self.candidate, base.event_id, time.monotonic() + self.promotion_timeout)
            with httpx.Client(
                headers={"Authorization": f"Bearer {self.service_token}"},
                timeout=5,
                transport=DeadlineTransport(cancelled=self.cancel.is_set),
            ) as topology:
                response = topology.get(
                    self.endpoints[self.candidate.region].topology + "/v1/topology",
                    extensions={"absolute_deadline": time.monotonic() + self._remaining(5)},
                )
                response.raise_for_status()
                if response.json().get("promoted"):
                    raise RuntimeError("Candidate was already promoted before the incident")
                # Allow an actual manager probe after replica synchronization; no direct promotion call.
                self.cancel.wait(min(2.5, self._remaining()))
                self._remaining()
                self.network.apply()
                output = self._sql(
                    self.writer, "SELECT ID FROM information_schema.PROCESSLIST WHERE USER='replication';"
                )
                for line in output.splitlines():
                    if line.isdecimal():
                        self._sql(self.writer, f"KILL CONNECTION {int(line)};")
                self._remaining()
                if not original.submit(old, epoch=epoch, effects=expected_effects(old, self.subscriptions)):
                    raise RuntimeError("Original writer's unreplicated suffix did not commit")
                deadline = time.monotonic() + self.promotion_timeout
                while time.monotonic() < deadline:
                    response = topology.get(
                        self.endpoints[self.candidate.region].topology + "/v1/topology",
                        extensions={"absolute_deadline": time.monotonic() + self._remaining(5)},
                    )
                    response.raise_for_status()
                    if response.json().get("promoted"):
                        break
                    self.cancel.wait(min(0.25, self._remaining()))
                else:
                    raise RuntimeError("Real database probe failure did not trigger configured regional promotion")
            promoted_at = time.time_ns()
            if self.present(self.candidate, old.event_id):
                raise RuntimeError("The claimed partition did not halt the established replication path")
            self._remaining()
            if not promoted.submit(new, epoch=epoch, effects=expected_effects(new, self.subscriptions)):
                raise RuntimeError("Promoted writer's divergent revision did not commit")
            for index in range(suffix_operations):
                self._remaining()
                payload = {
                    "title": f"Connection reuse review {index + 1}",
                    "body": "Track connection reuse and bounded retry behavior for gateway requests",
                    "state": "open",
                }
                operation = Operation(
                    str(uuid4()),
                    self.account.tenant_id,
                    str(uuid4()),
                    self.account.project_id,
                    1,
                    "issue.create",
                    canonical(payload),
                    self.account.owner_id,
                )
                if not promoted.submit(operation, epoch=epoch, effects=expected_effects(operation, self.subscriptions)):
                    raise RuntimeError("Promoted regional suffix request was not acknowledged")
            self.network.restore()
            restored_at = time.time_ns()
            # Divergent accepted histories must still exist after the physical connection is restored.
            old_retained = self.present(self.writer, old.event_id) and not self.present(self.writer, new.event_id)
            new_retained = self.present(self.candidate, new.event_id) and not self.present(self.candidate, old.event_id)
            if not old_retained or not new_retained:
                raise RuntimeError("Both actual accepted histories were not retained at handoff")
            rows = self._json(
                self.candidate,
                "SELECT JSON_OBJECT('count',COUNT(*)) FROM codehub.outbox WHERE state!='done';",
            )
            pending = rows[0]["count"] if rows else 0
            if pending < 1:
                raise RuntimeError("The incident did not retain an actual unprocessed regional backlog")
            self.ledger.close_epoch(epoch)
            self.evidence = DivergenceEvidence(
                self.group.name,
                issue,
                base.event_id,
                old.event_id,
                new.event_id,
                old_retained,
                new_retained,
                promoted_at,
                restored_at,
                pending,
            )
            (self.private_dir / "incident.json").write_text(canonical(asdict(self.evidence)), encoding="utf-8")
            return self.evidence
        finally:
            if restored_at is None:
                self.network.restore()
            original.close()
            promoted.close()

    def stop(self):
        self.network.restore()
