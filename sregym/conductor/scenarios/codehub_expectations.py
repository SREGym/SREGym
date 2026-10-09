"""Assemble private recovery expectations from owner receipts and seed facts.

This SREGym benchmark module never reads a workload database or repository to
define expected answers. Temporary effect sorting remains in the trusted owner;
only immutable count/hash and endpoint DTOs enter the verifier snapshot.
"""

import hashlib
import json
import re
import shutil
import sqlite3
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import UUID

from sregym.conductor.oracles.codehub_state import (
    KINDS,
    SEARCH_TYPES,
    EffectReceiptCut,
    ProtectedReceiptCut,
)
from sregym.conductor.oracles.regional_database_recovery import (
    DeliveryObserverTarget,
    FreshAPIChallenge,
    GitBundleExpectation,
    GitFileExpectation,
    GitProjectExpectation,
    GitRefExpectation,
    HTTPServiceTarget,
    RecoveryOutcomePlan,
    SQLTarget,
    WebhookDestination,
)
from sregym.conductor.scenarios.codehub_contracts import (
    DatasetManifest,
    DigestEntry,
    EntityCount,
    OperationCount,
    RecoverySource,
    SizeDistribution,
    StorageFootprint,
)
from sregym.generators.workload.codehub import (
    ReceiptLedger,
    canonical,
    operation_from_row,
    validate_effects,
    validate_git_provenance,
)
from sregym.generators.workload.codehub_seed import TenantAccount


def _label(value):
    if type(value) is not str or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", value):
        raise ValueError("Expected a private inventory DNS label")


def _identity(value):
    if type(value) is not str or str(UUID(value)) != value:
        raise ValueError("Expected a canonical private identity")


def _routing(routing):
    if type(routing) is not tuple or not routing:
        raise ValueError("Frozen tenant routing must be a nonempty tuple")
    result = {}
    for entry in routing:
        if type(entry) is not tuple or len(entry) != 2:
            raise ValueError("Frozen tenant routing requires tenant/group pairs")
        tenant, group = entry
        _identity(tenant)
        _label(group)
        if tenant in result:
            raise ValueError("Frozen routing cannot repeat a tenant")
        result[tenant] = group
    return result


def _accounts(tenants):
    if type(tenants) is not tuple or not tenants or any(type(item) is not TenantAccount for item in tenants):
        raise ValueError("Expected immutable private TenantAccount metadata")
    if len({item.tenant_id for item in tenants}) != len(tenants) or len({item.project_id for item in tenants}) != len(
        tenants
    ):
        raise ValueError("Seed tenant and project identities must be unique")
    for account in tenants:
        for identity in (account.tenant_id, account.project_id, account.owner_id, account.webhook_id):
            _identity(identity)
        _label(account.region)
        _label(account.group)
        if type(account.owner_token) is not str or len(account.owner_token) < 32:
            raise ValueError("Seed metadata lacks ordinary customer credentials")
    return tenants


def build_dataset_manifest(ledger, tenants, cuts, databases, physical, *, queue_count, oldest_queue_age_seconds):
    """Capture owner history metadata; source anchoring is qualified by the oracle.

    This sidecar never changes expected cuts or replaces them with observed SQL.
    A recovery-source digest names its protected payload history, not a hash of
    database files or a claim that a mutable server passed verification.
    """
    accounts = _accounts(tuple(tenants))
    by_tenant = {account.tenant_id: account for account in accounts}
    epochs = tuple(sorted({epoch for cut in cuts for epoch in cut.closed_epochs}))
    if not epochs or type(ledger) is not ReceiptLedger:
        raise ValueError("Dataset metadata requires the immutable owner receipt cut")
    scope = "r.status='acknowledged' AND r.epoch IN (" + ",".join("?" for _ in epochs) + ")"
    counts, mix = [], []
    histogram, digesters, operations = Counter(), {cut.group: hashlib.sha256() for cut in cuts}, Counter()
    with ledger._lock:
        for tenant, body in ledger._db.execute(
            "SELECT r.tenant_id,r.body FROM requests r WHERE " + scope + " ORDER BY r.event_id", epochs
        ):
            if tenant not in by_tenant:
                raise ValueError("Dataset receipts omit frozen tenant routing")
            encoded = body.encode("utf-8")
            if len(encoded) > 132 * 1024:
                raise ValueError("Dataset receipt exceeds the bounded operation size")
            histogram[len(encoded)] += 1
            group = by_tenant[tenant].group
            digesters[group].update(encoded + b"\n")
            operations[group] += 1
        for tenant, kind, count in ledger._db.execute(
            "SELECT r.tenant_id,substr(json_extract(r.body,'$.kind'),1,instr(json_extract(r.body,'$.kind'),'.')-1),"
            "COUNT(DISTINCT r.record_key) FROM requests r WHERE " + scope + " GROUP BY r.tenant_id,2",
            epochs,
        ):
            counts.append(EntityCount(kind, tenant, by_tenant[tenant].region, count))
        for tenant, project, kind, count in ledger._db.execute(
            "SELECT r.tenant_id,json_extract(r.body,'$.project_id'),json_extract(r.body,'$.kind'),COUNT(*) "
            "FROM requests r WHERE " + scope + " GROUP BY r.tenant_id,2,3 ORDER BY r.tenant_id,2,3",
            epochs,
        ):
            account = by_tenant[tenant]
            mix.append(OperationCount(kind, tenant, project, account.group, account.region, count))
    for cut in cuts:
        if operations[cut.group] != cut.operations or digesters[cut.group].hexdigest() != cut.journal_sha256:
            raise ValueError("Dataset metadata differs from the original protected receipt watermark")
    for account in accounts:
        counts.append(
            EntityCount(
                "user",
                account.tenant_id,
                account.region,
                len({identity for identity in (account.owner_id, account.reviewer_id) if identity}),
            )
        )
    count = sum(histogram.values())
    if not count:
        raise ValueError("Dataset metadata requires accepted business records")
    ordered = sorted(histogram)

    def quantile(numerator):
        target, seen = (count * numerator + 99) // 100, 0
        for size in ordered:
            seen += histogram[size]
            if seen >= target:
                return size
        raise ValueError("Dataset size distribution is incomplete")

    cuts_by_group = {cut.group: cut for cut in cuts}
    return DatasetManifest(
        2,
        tuple(sorted(counts, key=lambda item: (item.region, item.tenant, item.entity))),
        count,
        tuple(DigestEntry(cut.group, cut.journal_sha256) for cut in sorted(cuts, key=lambda item: item.group)),
        StorageFootprint(**physical["storage"]),
        queue_count,
        oldest_queue_age_seconds,
        (SizeDistribution("accepted_operation_bytes", count, ordered[0], quantile(50), quantile(95), ordered[-1]),),
        tuple(
            RecoverySource(
                member.name,
                member.region,
                "database",
                cuts_by_group[group.name].journal_sha256,
                cuts_by_group[group.name].operations,
                True,
            )
            for group in databases
            for member in group.members
        ),
        tuple(mix),
    )


def build_receipt_cuts(ledger, routing):
    """Copy one coherent closed watermark without copying large effect payloads."""
    if type(ledger) is not ReceiptLedger:
        raise ValueError("Cut assembly requires the trusted owner ReceiptLedger")
    routes = _routing(routing)
    # The existing owner lock is reentrant. Live noise may continue submitting
    # requests; closure and this private snapshot cannot interleave.
    with ledger._lock:
        raw_cuts = ledger.partition_cuts(routes)
        groups = set(routes.values())
        if set(raw_cuts) != groups:
            raise ValueError("Owner receipt cuts omit declared database groups")
        cuts = tuple(ProtectedReceiptCut.from_receipt_cut(group, raw_cuts[group]) for group in sorted(groups))
        coverage = {}
        for cut in cuts:
            for key in cut.record_keys:
                tenant = key.split("/", 1)[0]
                if routes.get(tenant) != cut.group:
                    raise ValueError("Protected receipts differ from frozen tenant routing")
                coverage[tenant] = cut.group
        if set(coverage) != set(routes):
            raise ValueError("Closed receipts omit declared tenants")
        return cuts, _effect_cuts(ledger, routes, cuts)


def _effect_cuts(ledger, routes, cuts):
    accepted, kinds = Counter(), {group: set() for group in routes.values()}
    byte_budget = min(8 * 1024**3, ledger._byte_budget // 4)
    guard = getattr(ledger, "_snapshot_guard", lambda: 0)
    if guard() or shutil.disk_usage(ledger._path.parent).free < byte_budget + 256 * 1024**2:
        raise RuntimeError("Private effect snapshot capacity or cancellation unavailable")
    with TemporaryDirectory(prefix="private-effects-", dir=ledger._path.parent) as temporary:
        path = Path(temporary) / "effects.sqlite3"
        db = sqlite3.connect(path)
        try:
            path.chmod(0o600)
            last_check, interrupted = 0.0, False

            def progress():
                nonlocal last_check, interrupted
                now = time.monotonic()
                if guard():
                    interrupted = True
                    return 1
                if now >= last_check:
                    last_check = now + 1
                    used = sum(item.stat().st_size for item in Path(temporary).iterdir() if item.is_file())
                    if used >= byte_budget or shutil.disk_usage(temporary).free < 256 * 1024**2:
                        interrupted = True
                        return 1
                return 0

            db.set_progress_handler(progress, 1000)
            page_size = db.execute("PRAGMA page_size").fetchone()[0]
            db.execute(f"PRAGMA max_page_count={byte_budget // (2 * page_size)}")
            db.executescript(
                "PRAGMA temp_store=MEMORY; PRAGMA cache_size=-16384; CREATE TABLE effects(id TEXT PRIMARY KEY,group_id TEXT NOT NULL,body TEXT NOT NULL); CREATE INDEX sorted_effects ON effects(group_id,id);"
            )
            rows = ledger._db.execute(
                "SELECT r.tenant_id,r.body,x.effects FROM requests r JOIN epochs e ON e.id=r.epoch "
                "LEFT JOIN expectations x USING(event_id) WHERE e.closed=1 AND r.status='acknowledged'"
            )
            for tenant, body, expected in rows:
                if expected is None:
                    raise ValueError("An acknowledged owner receipt has no effect expectations")
                operation = operation_from_row(json.loads(body))
                if operation.tenant_id != tenant or tenant not in routes or operation.kind not in KINDS:
                    raise ValueError("Effect provenance omits a routed tenant")
                group = routes[tenant]
                effects = validate_effects(operation, json.loads(expected))
                by_kind = Counter(effect["effect_kind"] for effect in effects)
                requires_search = operation.kind.split(".", 1)[0] in SEARCH_TYPES
                payload = operation.request()["payload"]
                requires_build = operation.kind in {"repository.push", "change.create", "change.update"} and bool(
                    payload.get("commit_sha") or payload.get("head_sha")
                )
                if by_kind["search"] != int(requires_search) or by_kind["build"] != int(requires_build):
                    raise ValueError("Applicable search/build expectations are missing or duplicated")
                accepted[group] += 1
                for effect in effects:
                    kinds[group].add(effect["effect_kind"])
                    try:
                        db.execute(
                            "INSERT INTO effects VALUES (?,?,?)", (effect["effect_id"], group, canonical(effect))
                        )
                    except sqlite3.IntegrityError as exc:
                        raise ValueError("Independent effect identities cannot repeat across receipts") from exc
            if accepted != Counter({cut.group: cut.operations for cut in cuts}):
                raise ValueError("Effect expectations differ from the closed receipt watermark")
            result = []
            for group in sorted(kinds):
                if kinds[group] != {"search", "build", "delivery"}:
                    raise ValueError("Each seeded group needs real search, build and delivery expectations")
                checksum, count = hashlib.sha256(), 0
                for (body,) in db.execute("SELECT body FROM effects WHERE group_id=? ORDER BY id", (group,)):
                    checksum.update(body.encode() + b"\n")
                    count += 1
                result.append(EffectReceiptCut(group, count, checksum.hexdigest()))
            return tuple(result)
        except sqlite3.OperationalError as error:
            if interrupted or "full" in str(error).lower():
                raise RuntimeError("Private effect snapshot capacity or cancellation unavailable") from error
            raise
        finally:
            db.close()


def _git_entries(entries):
    if type(entries) is not tuple or not entries:
        raise ValueError("Git expectations need closed acknowledged owner provenance")
    result, events = [], set()
    for entry in entries:
        if type(entry) is not dict or set(entry) != {"operation", "provenance"}:
            raise ValueError("Git provenance requires its complete accepted operation")
        operation = operation_from_row(entry["operation"])
        provenance = validate_git_provenance(operation, entry["provenance"])
        if operation.event_id in events:
            raise ValueError("Git operation identities cannot repeat")
        events.add(operation.event_id)
        result.append((operation, provenance))
    return tuple(result)


def _git_inventory(entries):
    projects, owners = {}, {}
    for operation, provenance in _git_entries(entries):
        project = operation.project_id
        known = projects.setdefault(project, {"refs": {}, "files": {}, "bundles": {}, "history": {}})
        commit = operation.request()["payload"]["commit_sha"]
        ref = operation.request()["payload"]["ref"]
        owner = (operation.tenant_id, operation.entity_id)
        if owners.setdefault((project, ref), owner) != owner:
            raise ValueError("A repository branch cannot change its accepted entity identity")
        history = known["history"].setdefault(ref, {})
        if operation.client_revision in history:
            raise ValueError("A repository branch revision has conflicting accepted receipts")
        history[operation.client_revision] = commit
        latest = max(history)
        known["refs"][ref] = history[latest]
        files = {item["path"]: item["sha256"] for item in provenance["files"]}
        if commit in known["files"] and known["files"][commit] != files:
            raise ValueError("A Git commit cannot acquire different file contents")
        known["files"][commit] = files
        checksum = provenance["bundles"][0]["sha256"]
        if commit in known["bundles"] and known["bundles"][commit] != checksum:
            raise ValueError("A Git commit cannot acquire a different build bundle")
        known["bundles"][commit] = checksum
    return projects


def _project(project_id, token, inventory):
    return GitProjectExpectation(
        project_id,
        token,
        tuple(GitRefExpectation(ref, commit) for ref, commit in sorted(inventory["refs"].items())),
        tuple(
            GitFileExpectation(commit, path, checksum)
            for commit, files in sorted(inventory["files"].items())
            for path, checksum in sorted(files.items())
        ),
        tuple(GitBundleExpectation(commit, checksum) for commit, checksum in sorted(inventory["bundles"].items())),
    )


def compile_seed_git_inventory(tenants, entries):
    """Freeze seed refs and historical source hashes from independent push facts."""
    accounts = _accounts(tenants)
    inventory = _git_inventory(entries)
    if set(inventory) != {account.project_id for account in accounts}:
        raise ValueError("Seed Git provenance must cover every declared tenant project")
    tenant_by_project = {account.project_id: account.tenant_id for account in accounts}
    for operation, _provenance in _git_entries(entries):
        if operation.tenant_id != tenant_by_project[operation.project_id]:
            raise ValueError("Seed Git provenance belongs to a different tenant")
    result = []
    for account in sorted(accounts, key=lambda item: item.project_id):
        known = inventory[account.project_id]
        if known["refs"].get("refs/heads/main") != account.git_commit:
            raise ValueError("Seed main reference differs from independently captured seed metadata")
        if (
            type(account.git_files) is not tuple
            or not account.git_files
            or any(type(item) is not tuple or len(item) != 2 for item in account.git_files)
        ):
            raise ValueError("Seed main source inventory must be immutable and nonempty")
        if len(dict(account.git_files)) != len(account.git_files) or dict(account.git_files) != known["files"].get(
            account.git_commit
        ):
            raise ValueError("Seed main source hashes differ from independent push provenance")
        result.append(_project(account.project_id, account.owner_token, known))
    return tuple(result)


def merge_git_provenance(seed, entries):
    """Keep all frozen content; advance refs only through their captured history."""
    if type(seed) is not tuple or not seed or any(type(item) is not GitProjectExpectation for item in seed):
        raise ValueError("Git merging requires immutable independent seed expectations")
    if len({item.project_id for item in seed}) != len(seed):
        raise ValueError("Seed Git projects must be unique")
    for project in seed:
        files = {item.commit_sha for item in project.files}
        if (
            files != {item.commit_sha for item in project.bundles}
            or not {item.commit_sha for item in project.refs} <= files
        ):
            raise ValueError("Every seeded reference and build commit needs independent source and bundle hashes")
    if entries == ():
        return seed
    additions = _git_inventory(entries)
    if not set(additions) <= {item.project_id for item in seed}:
        raise ValueError("Git additions cannot introduce an undeclared project")
    result = []
    for project in seed:
        known = additions.get(project.project_id)
        if known is None:
            result.append(project)
            continue
        original_files = {}
        for item in project.files:
            original_files.setdefault(item.commit_sha, {})[item.path] = item.sha256
        files = dict(original_files)
        bundles = {item.commit_sha: item.sha256 for item in project.bundles}
        refs = {item.ref: item.commit_sha for item in project.refs}
        for commit, contents in known["files"].items():
            if commit in files and files[commit] != contents:
                raise ValueError("Captured provenance cannot replace frozen Git source hashes")
            files[commit] = contents
        for commit, checksum in known["bundles"].items():
            if commit in bundles and bundles[commit] != checksum:
                raise ValueError("Captured provenance cannot replace frozen build hashes")
            bundles[commit] = checksum
        for ref, commit in known["refs"].items():
            if ref in refs and refs[ref] != commit:
                history = known["history"][ref]
                baseline_revisions = [revision for revision, value in history.items() if value == refs[ref]]
                if not baseline_revisions or max(history) <= max(baseline_revisions):
                    # Older seed facts cannot roll back an independently frozen
                    # current ref, and a fresh branch cannot hijack that identity.
                    raise ValueError("A frozen Git ref cannot be overwritten without its accepted history")
            refs[ref] = commit
        result.append(_project(project.project_id, project.token, {"refs": refs, "files": files, "bundles": bundles}))
    return tuple(result)


@dataclass(frozen=True)
class DatabaseMemberRequirement:
    group: str
    region: str
    namespace: str
    service: str

    def __post_init__(self):
        for value in (self.group, self.region, self.namespace, self.service):
            _label(value)


@dataclass(frozen=True)
class RegionReplicaRequirement:
    region: str
    namespace: str
    api_replicas: int
    search_replicas: int
    repository_replicas: int
    gateway_replicas: int = 2

    def __post_init__(self):
        _label(self.region)
        _label(self.namespace)
        for value in (self.api_replicas, self.search_replicas, self.repository_replicas, self.gateway_replicas):
            if type(value) is not int or value < 1:
                raise ValueError("Every declared region needs positive replica counts")


@dataclass(frozen=True)
class RegionalTargetInventory:
    databases: tuple[SQLTarget, ...]
    api_targets: tuple[HTTPServiceTarget, ...]
    search_targets: tuple[HTTPServiceTarget, ...]
    repository_targets: tuple[HTTPServiceTarget, ...]
    expected_sql: tuple[DatabaseMemberRequirement, ...]
    regions: tuple[RegionReplicaRequirement, ...]
    gateway_targets: tuple[HTTPServiceTarget, ...] = ()

    def __post_init__(self):
        for name, kind in (
            ("databases", SQLTarget),
            ("api_targets", HTTPServiceTarget),
            ("search_targets", HTTPServiceTarget),
            ("repository_targets", HTTPServiceTarget),
            ("expected_sql", DatabaseMemberRequirement),
            ("regions", RegionReplicaRequirement),
        ):
            values = getattr(self, name)
            if type(values) is not tuple or not values or any(type(item) is not kind for item in values):
                raise ValueError("Replica inventory must contain immutable typed targets and requirements")
        if (
            len(self.regions) < 2
            or len({item.region for item in self.regions}) != len(self.regions)
            or len({item.namespace for item in self.regions}) != len(self.regions)
        ):
            raise ValueError("Regional inventory needs distinct regions and namespaces")
        sql = {(item.group, item.region, item.namespace, item.service) for item in self.databases}
        required = {(item.group, item.region, item.namespace, item.service) for item in self.expected_sql}
        if len(sql) != len(self.databases) or len(required) != len(self.expected_sql) or sql != required:
            raise ValueError("All declared database replicas must be targeted exactly once")
        if len({(item.namespace, item.service) for item in self.databases}) != len(self.databases):
            raise ValueError("Database observation targets must be independent")
        declared = {item.region: item for item in self.regions}
        for item in self.databases:
            if item.region not in declared or item.namespace != declared[item.region].namespace:
                raise ValueError("Database observation targets differ from declared regions")
        for group in {item.group for item in self.databases}:
            members = [item for item in self.databases if item.group == group]
            if len(members) < 4 or len({item.region for item in members}) < 2:
                raise ValueError("Each group needs four independent members across two regions")
        if type(self.gateway_targets) is not tuple or any(
            type(t) is not HTTPServiceTarget for t in self.gateway_targets
        ):
            raise ValueError("Gateway targets must be immutable typed observations")
        for role in ("api", "search", "repository", "gateway"):
            targets = getattr(self, f"{role}_targets")
            if role == "gateway" and not targets:
                continue
            if len({(item.namespace, item.service, item.port) for item in targets}) != len(targets):
                raise ValueError("A shared endpoint cannot stand in for multiple replicas")
            counts = Counter()
            for item in targets:
                counts[item.region] += item.expected_replicas or 1
            if (
                set(counts) != set(declared)
                or any(counts[name] != getattr(region, f"{role}_replicas") for name, region in declared.items())
                or any(item.namespace != declared[item.region].namespace for item in targets)
            ):
                raise ValueError("Every declared application replica must have an observation target")


@dataclass(frozen=True)
class WebhookSubscription:
    tenant_id: str
    destination: WebhookDestination

    def __post_init__(self):
        _identity(self.tenant_id)
        if type(self.destination) is not WebhookDestination:
            raise ValueError("Expected immutable normal webhook subscription metadata")


def build_recovery_outcomes(
    tenants, routing, *, seed_projects, git_entries, inventory, observer, service_token, webhooks
):
    accounts, routes = _accounts(tenants), _routing(routing)
    if type(observer) is not DeliveryObserverTarget or observer.transport != "private_pipe":
        raise ValueError("Production recovery observations require the private owner receipt pipe")
    if {item.tenant_id: item.group for item in accounts} != routes:
        raise ValueError("Tenant metadata and frozen routing must cover the same groups")
    if type(inventory) is not RegionalTargetInventory or {item.group for item in inventory.databases} != set(
        routes.values()
    ):
        raise ValueError("All routed groups need the complete declared replica inventory")
    if not {item.region for item in accounts} <= {item.region for item in inventory.regions}:
        raise ValueError("Tenant metadata names an undeclared region")
    if type(webhooks) is not tuple or any(type(item) is not WebhookSubscription for item in webhooks):
        raise ValueError("Normal subscription metadata must be immutable")
    subscriptions = {}
    for item in webhooks:
        if item.tenant_id not in routes or any(
            hook.id == item.destination.id for hook in subscriptions.get(item.tenant_id, ())
        ):
            raise ValueError("Subscriptions have unknown tenants or duplicate identities")
        subscriptions.setdefault(item.tenant_id, []).append(item.destination)
    for account in accounts:
        if not any(hook.id == account.webhook_id for hook in subscriptions.get(account.tenant_id, ())):
            raise ValueError("Seed subscription metadata omits a declared tenant webhook")
    projects = merge_git_provenance(seed_projects, git_entries)
    if {item.project_id for item in projects} != {item.project_id for item in accounts}:
        raise ValueError("Independent Git expectations omit declared tenant projects")
    tenant_by_project = {item.project_id: item.tenant_id for item in accounts}
    if git_entries:
        for operation, _provenance in _git_entries(git_entries):
            if operation.tenant_id != tenant_by_project[operation.project_id]:
                raise ValueError("Fresh Git provenance belongs to a different tenant")
    challenges = tuple(
        FreshAPIChallenge(
            item.group,
            item.tenant_id,
            item.project_id,
            item.owner_id,
            item.owner_token,
            tuple(subscriptions[item.tenant_id]),
        )
        for item in sorted(accounts, key=lambda item: item.tenant_id)
    )
    return RecoveryOutcomePlan(
        inventory.api_targets,
        inventory.search_targets,
        inventory.repository_targets,
        projects,
        challenges,
        observer,
        service_token,
        inventory.gateway_targets,
    )
