"""A partition between two MySQL regions ends with a promoted replica and two histories.

Modelled on GitHub's 21 October 2018 incident. While the regions cannot reach
each other, Gitea keeps writing issues, comments and closes to the east primary,
and their webhook events pile up in the relay, which cannot reach the west
consumer. Delivery is paused, the west replica is promoted, writes are routed
there, and new issues reuse the numbers east already handed out, so the backlog
mixes current events with events that describe the old history.
"""

import json
import secrets
import time

from sregym.conductor.oracles.apps.gitea.mysql_failover import GiteaMySQLFailoverOracle, delivery_report, issue_events
from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.problems.base import Problem
from sregym.service.apps.gitea.application import FIXTURES
from sregym.service.apps.gitea.mysql_failover import (
    DELIVER_TIMEOUT,
    LIVE_TRAFFIC,
    RECEIVER,
    GiteaMySQLApplication,
)
from sregym.utils.decorators import mark_fault_injected

#: Consecutive failed probes before the replica is promoted.
FAILOVER_PROBES = 3
SUBJECTS = (
    "Login page times out on slow connections",
    "Export to CSV drops the last row",
    "Search ignores archived projects",
    "Typo in the onboarding email",
    "Webhook settings page does not save",
    "Dark mode contrast too low on tables",
    "Pagination resets after editing a filter",
    "Upload fails for files with spaces in the name",
    "Notification badge count is off by one",
    "Mobile layout overlaps the sidebar",
    "API returns 500 for empty labels",
    "Date picker shows the wrong week start",
)
REMARKS = (
    "Reproduced on the latest release",
    "Still happening after clearing the cache",
    "Customer confirmed the workaround helps",
    "Seen again in the nightly run",
    "Attached logs from the affected tenant",
    "Could be related to the last migration",
)


class GiteaMySQLRegionalFailoverProblem(Problem):
    cleanup_timeout_seconds = 1200

    def __init__(self, scale_tier="small"):
        super().__init__(GiteaMySQLApplication(tier=scale_tier))
        self.kubectl = self.app.kubectl
        self.faulty_service, self.expected_service_port = "gitea", 3000
        self.shared = None
        self.shared_receipts = []
        self.orphaned = []
        self.accepted_after_promotion = []
        self.updates = []
        self.expected_git = None
        self.repository_ids = {}
        self.receiver = None
        self.live_users = None
        self.failover_performed = False
        self.root_cause = self.build_structured_root_cause(
            component="gitea-db",
            namespace=self.namespace,
            description="A network partition between region-east and region-west made the failover promote the "
            "west MySQL replica. The east primary had acknowledged issues, comments and closes during the partition "
            "that never replicated, and their webhook events queued in the relay, which could not reach the west "
            "receiver. Delivery was then paused. After promotion, users resubmitted some lost issues through Gitea, "
            "and west also accepted independent new issues and comments. West reused issue numbers east had already "
            "handed out. The paused backlog therefore mixes events from both histories, including repeated creation "
            "events for resubmitted issues. Recovery must keep both histories, reconcile resubmissions without "
            "duplicating logical issues, drain the backlog so each logical event "
            "arrives once under the issue's current number, and rebuild east as a clean replica of west without "
            "interrupting the live west primary or Gitea.",
        )
        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)
        self.mitigation_oracle = GiteaMySQLFailoverOracle(problem=self)
        self.app.create_workload()

    def records(self, count):
        """Ordinary-looking issues; which phase wrote them is known only to the conductor."""
        fixture = json.loads((FIXTURES / "import-data.json").read_text())
        repositories = [r["owner"] + "/" + r["name"] for r in fixture["repositories"]]
        records = []
        for _ in range(count):
            reference = secrets.token_hex(3)
            records.append(
                {
                    "repository": secrets.choice(repositories),
                    "title": f"{SUBJECTS[secrets.randbelow(len(SUBJECTS))]} ({reference})",
                    "body": f"Reported by a customer, ticket {reference}.",
                    "reference": reference,
                }
            )
        return records

    @staticmethod
    def updates_for(issues, comments, closes):
        """Comments on and closes of distinct issues drawn from ``issues``."""
        chosen = sorted(issues, key=lambda _: secrets.randbits(32))
        updates = []
        for issue in chosen[:comments]:
            reference = secrets.token_hex(3)
            updates.append({**issue, "comment": f"{secrets.choice(REMARKS)} ({reference}).", "reference": reference})
        updates += [{**issue, "close": True} for issue in chosen[comments : comments + closes]]
        return updates

    def require_delivered(self, region, issues, updates):
        events, lost = issue_events(self.app.snapshot(region), issues, updates)
        report = delivery_report(self.app.webhook_deliveries(), events)
        if lost or any(report.values()):
            raise RuntimeError(f"Shared webhook deliveries are not exactly once: {lost} {report}")

    def require_queued(self, region, issues, updates):
        events, lost = issue_events(self.app.snapshot(region), issues, updates)
        # A resubmitted issue gets the id and number its lost original had, so both events may be queued.
        queued = delivery_report(self.app.relay()["queued"], events)
        delivered = delivery_report(self.app.webhook_deliveries(), events)
        if lost or queued["undelivered"] or len(delivered["undelivered"]) != len(events):
            raise RuntimeError(f"Post-failover events are not held in the paused relay: {lost} {queued}")

    def wait_for_partition(self, timeout=120):
        app, mysql = self.app, self.app.mysql
        failures = 0
        deadline = time.monotonic() + timeout
        while failures < FAILOVER_PROBES:
            if time.monotonic() >= deadline:
                raise RuntimeError("The partition did not isolate the regions")
            # An established replication connection survives a new policy until it is redialled.
            connected = (mysql.replication("west") or {}).get("io") == "ON"
            if connected:
                mysql.reconnect("west")
            cut = not connected and not mysql.reachable("west", "east") and not app.receiver_reachable()
            failures = failures + 1 if cut else 0
            time.sleep(5)

    @mark_fault_injected
    def inject_fault(self):
        if self.shared is not None:
            raise RuntimeError("Incident divergence material already exists")
        app, mysql = self.app, self.app.mysql
        shared_count, orphan_count, post_count = app.divergence_counts

        self.shared_receipts = app.recovery_client("create", records=self.records(shared_count))
        app.wait_for_webhook_attempts("east")
        mysql.wait_for_gtids("west", mysql.gtid_executed("east"))
        if app.undelivered_webhooks("west"):
            raise RuntimeError("West has not seen every shared webhook attempt")
        if app.wait_for_relay()["queued"]:
            raise RuntimeError("The relay did not forward the shared webhooks")
        self.require_delivered("east", self.shared_receipts, [])
        self.shared = app.snapshot("east")
        self.repository_ids = app.repository_ids()

        self.failover_performed = True
        app.apply(mysql.partition_documents(self.namespace))
        self.wait_for_partition()
        self.orphaned = app.recovery_client("create", records=self.records(orphan_count))
        partition_updates = app.recovery_client(
            "update",
            updates=self.updates_for(self.orphaned, orphan_count // 2, 0)
            + self.updates_for(self.shared_receipts, orphan_count // 2, orphan_count // 4),
        )
        app.wait_for_webhook_attempts("east")
        titles = {r["title"] for r in self.orphaned}
        remarks = {r["comment"] for r in partition_updates if "comment" in r}
        received = app.webhook_deliveries()
        if titles & {d["title"] for d in received} or remarks & {d["comment"] for d in received}:
            raise RuntimeError("Partition webhooks reached the receiver")
        west = app.snapshot("west")
        if titles & {r["name"] for r in west["issues"]} or remarks & {r["content"] for r in west["comments"]}:
            raise RuntimeError("Partition writes reached the west replica")
        if len(app.relay()["queued"]) < len(self.orphaned) + len(partition_updates):
            raise RuntimeError("Partition webhooks did not queue in the relay")
        # Delivery stops before the network heals; an attempt already in flight times out first.
        app.relay("pause")
        time.sleep(DELIVER_TIMEOUT + 1)
        mysql.promote("west")

        mysql.heal()
        mysql.route_writes("west")
        app.restart_gitea()

        # Users whose issues vanished open some of them again on the promoted primary.
        resubmitted = app.recovery_client(
            "create",
            records=[
                {k: r[k] for k in ("repository", "title", "body")} for r in self.orphaned[: max(2, orphan_count // 6)]
            ],
        )
        self.accepted_after_promotion = app.recovery_client("create", records=self.records(post_count))
        post_updates = app.recovery_client("update", updates=self.updates_for(self.shared_receipts, post_count // 2, 0))
        app.wait_for_webhook_attempts("west")
        self.require_queued("west", [*resubmitted, *self.accepted_after_promotion], post_updates)
        self.updates = partition_updates + post_updates
        collisions = {(r["repository"], r["number"]) for r in self.orphaned} & {
            (r["repository"], r["number"]) for r in self.accepted_after_promotion
        }
        if not collisions:
            raise RuntimeError("The two histories did not reuse any public issue number")
        self.expected_git = app.git_inventory()
        self.receiver = app.pod_identities(RECEIVER)
        if len(self.receiver) != 1:
            raise RuntimeError("Expected one webhook receiver pod")
        app.start_live_traffic()
        self.live_users = app.pod_identities(LIVE_TRAFFIC)

    @mark_fault_injected(strict=True)
    def recover_fault(self):
        self.app.mysql.heal()
        if self.failover_performed and not self.mitigation_oracle.evaluate().get("success"):
            self.app.wait_for_api()
            self.app.reference_repair()
        self.failover_performed = False
