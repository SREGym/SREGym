"""Require one replicated MySQL history, both regions' issue activity, and each event delivered once."""

import json
import secrets
import time
from collections import Counter

from sregym.conductor.oracles.apps.gitea.divergent_recovery import GiteaDivergentRecoveryOracle, gitea_keys
from sregym.conductor.oracles.apps.gitlab.divergent_recovery import divergence_report
from sregym.conductor.oracles.failure import FailureClass
from sregym.conductor.oracles.service_endpoint_mitigation import ServiceEndpointMitigationOracle
from sregym.service.apps.components.mysql import REGIONS, other
from sregym.service.apps.gitea.application import FIXTURES
from sregym.service.apps.gitea.mysql_failover import (
    LIVE_TRAFFIC,
    LIVE_TRAFFIC_REPOSITORY,
    RECEIVER,
    RELAY,
    repository_names,
)

REPOSITORY_VOLUME = "gitea-repositories"
#: Gitea indexes issues and comments asynchronously after each write.
SEARCH_TIMEOUT_SECONDS = 60
#: Longest stretch without an acknowledged user write that still counts as staying online.
MAX_OUTAGE_SECONDS = 10


def issue_events(state, issues, updates):
    """The events the primary's rows imply for each acknowledged write, or the writes it lost.

    Every key names the issue's current number, id and content, so a stale or
    invented payload never matches.
    """
    names = repository_names(state)
    rows = {(names[r["repo_id"]], r["name"], r["content"]): r for r in state["issues"]}
    comments = Counter((r["issue_id"], r["content"]) for r in state["comments"])
    comment_ids = {}
    for comment in state["comments"]:
        comment_ids.setdefault((comment["issue_id"], comment["content"]), []).append(comment["id"])
    events, lost = [], []
    for record in [*issues, *updates]:
        repository = record["repository"].lower()
        row = rows.get((repository, record["title"], record["body"]))
        if row is None:
            lost.append(record["title"])
            continue
        issue = (repository, row["index"], row["id"], row["name"], row["content"])
        if "comment" in record:
            if not comments[(row["id"], record["comment"])]:
                lost.append(record["comment"])
                continue
            comments[(row["id"], record["comment"])] -= 1
            comment_id = comment_ids[(row["id"], record["comment"])].pop(0)
            events.append(("issue_comment", "created", *issue, comment_id, record["comment"]))
        elif record.get("close"):
            if not row["is_closed"]:
                lost.append(record["title"])
                continue
            events.append(("issues", "closed", *issue, None, None))
        else:
            events.append(("issues", "opened", *issue, None, None))
    return events, lost


def search_queries(state, issues, updates):
    """Each stranded issue's and comment's reference, with the number its issue now has on the primary."""
    names = repository_names(state)
    numbers = {(names[r["repo_id"]], r["name"], r["content"]): r["index"] for r in state["issues"]}
    return [
        {
            "repository": record["repository"],
            "text": record["reference"],
            "number": numbers[(record["repository"].lower(), record["title"], record["body"])],
        }
        for record in [*issues, *(u for u in updates if "comment" in u)]
    ]


def live_traffic_report(traffic, issues, repository_id, graded=()):
    """Acknowledged user writes the primary no longer has, and the longest time no write was acknowledged.

    ``graded`` holds the windows in which the oracle itself paused the writers.
    """
    present = {(r["index"], r["name"]) for r in issues if r["repo_id"] == repository_id}
    times = [traffic["started"], *(w["at"] for w in traffic["acknowledged"]), traffic["now"]]

    def outage(a, b):
        return b - a - sum(max(0, min(b, end) - max(a, start)) for start, end in graded)

    return {
        "lost": [w["title"] for w in traffic["acknowledged"] if (w["number"], w["title"]) not in present],
        "longest_outage": max(outage(a, b) for a, b in zip(times, times[1:], strict=False)),
    }


def delivery_report(deliveries, events):
    """Each implied event must arrive exactly once; no delivery may describe a number's issue wrongly."""
    received = Counter(
        (
            d["event"],
            d["action"],
            (d["repository"] or "").lower(),
            d["number"],
            d["issue_id"],
            d["title"],
            d["body"],
            d["comment_id"],
            d["comment"],
        )
        for d in deliveries
    )
    issues = {(e[2], e[3]): e[4:7] for e in events}
    return {
        "undelivered": [e[5] if e[7] is None else e[8] for e in events if received[e] == 0],
        "duplicated": [e[5] if e[7] is None else e[8] for e in events if received[e] > 1],
        "conflicting": sorted(
            {
                f"{d['repository']}#{d['number']}"
                for d in deliveries
                if (key := ((d["repository"] or "").lower(), d["number"])) in issues
                and (d["issue_id"], d["title"], d["body"]) != issues[key]
            }
        ),
    }


class GiteaMySQLFailoverOracle(GiteaDivergentRecoveryOracle):
    FAILURE_CLASSES = {
        "gitea_requires_one_repository_writer": FailureClass.AGENT_ERROR,
        "database_route_bypassed": FailureClass.AGENT_ERROR,
        "repository_volume_replaced": FailureClass.AGENT_ERROR,
        "webhook_receiver_replaced": FailureClass.AGENT_ERROR,
        "webhook_relay_replaced": FailureClass.AGENT_ERROR,
        "webhook_backlog_not_drained": FailureClass.AGENT_ERROR,
        "live_traffic_replaced": FailureClass.AGENT_ERROR,
        "live_database_taken_down": FailureClass.AGENT_ERROR,
        "live_gitea_restarted": FailureClass.AGENT_ERROR,
        "service_interrupted": FailureClass.AGENT_ERROR,
        "live_writes_lost": FailureClass.AGENT_ERROR,
        "writable_primary_count": FailureClass.AGENT_ERROR,
        "write_service_not_on_primary": FailureClass.AGENT_ERROR,
        "replica_not_replicating": FailureClass.AGENT_ERROR,
        "replica_skips_changes": FailureClass.AGENT_ERROR,
        "regions_inconsistent": FailureClass.AGENT_ERROR,
        "issue_counter_behind": FailureClass.AGENT_ERROR,
        "issue_statistics_stale": FailureClass.AGENT_ERROR,
        "recovered_writes_not_indexed": FailureClass.AGENT_ERROR,
        "acknowledged_updates_lost": FailureClass.AGENT_ERROR,
        "webhook_events_conflicting": FailureClass.AGENT_ERROR,
        "webhook_events_undelivered": FailureClass.AGENT_ERROR,
        "webhook_events_duplicated": FailureClass.AGENT_ERROR,
    }

    graded = ()

    def capture_baseline(self):
        self.volumes = self.volume_ids()
        if len(self.volumes) != self.problem.app.expected_volume_count:
            raise RuntimeError("The application does not have its expected persistent volumes")
        mysql = self.problem.app.mysql
        self.baseline = self.problem.app.check_workflow()
        if not mysql.converged("west", "east"):
            raise RuntimeError("The west replica has not converged with the east primary")

    def check_resources(self):
        app = self.problem.app
        deployment = json.loads(app.command("get", "deployment", "gitea", "-o", "json"))
        if deployment["spec"].get("replicas", 1) != 1:
            return self.fail("gitea_requires_one_repository_writer")
        env = {e["name"]: e.get("value") for e in deployment["spec"]["template"]["spec"]["containers"][0]["env"]}
        if env.get("GITEA__database__HOST") != f"{app.mysql.write_service}:3306":
            return self.fail("database_route_bypassed", host=env.get("GITEA__database__HOST"))
        if self.volume_ids().get(REPOSITORY_VOLUME) != self.volumes[REPOSITORY_VOLUME]:
            return self.fail("repository_volume_replaced")
        if self.problem.receiver is not None and app.pod_identities(f"app={RECEIVER}") != self.problem.receiver:
            return self.fail("webhook_receiver_replaced")
        if self.problem.relay is not None and app.pod_identities(f"app={RELAY}") != self.problem.relay:
            return self.fail("webhook_relay_replaced")
        if self.problem.live_users is not None and app.pod_identities(f"app={LIVE_TRAFFIC}") != self.problem.live_users:
            return self.fail("live_traffic_replaced")
        if self.problem.live_database is not None and app.database_identity("west") != self.problem.live_database:
            return self.fail("live_database_taken_down")
        # One Gitea replica with the Recreate strategy: any restart is downtime.
        if self.problem.live_gitea is not None and app.pod_identities("app=gitea") != self.problem.live_gitea:
            return self.fail("live_gitea_restarted")
        return None

    def check_search(self, primary):
        """Writes made through Gitea are searchable; rows written around it are not."""
        problem, app = self.problem, self.problem.app
        queries = search_queries(app.snapshot(primary), problem.orphaned, problem.updates)
        deadline = time.monotonic() + SEARCH_TIMEOUT_SECONDS
        while True:
            found = app.recovery_client("search", queries=queries)
            missing = [q["text"] for q, numbers in zip(queries, found, strict=True) if q["number"] not in numbers]
            if not missing:
                return None
            if time.monotonic() >= deadline:
                return self.fail("recovered_writes_not_indexed", references=missing)
            time.sleep(5)

    def check_live_traffic(self, primary, traffic):
        problem, app = self.problem, self.problem.app
        report = live_traffic_report(
            traffic, app.snapshot(primary)["issues"], problem.repository_ids[LIVE_TRAFFIC_REPOSITORY], self.graded
        )
        if report["longest_outage"] > MAX_OUTAGE_SECONDS:
            return self.fail("service_interrupted", seconds=round(report["longest_outage"]))
        if report["lost"]:
            return self.fail("live_writes_lost", writes=report["lost"])
        return None

    def check_replication(self):
        mysql = self.problem.app.mysql
        writable = [region for region in REGIONS if mysql.writable(region)]
        if len(writable) != 1:
            return None, self.fail("writable_primary_count", writable=writable)
        primary = writable[0]
        replica = other(primary)
        if mysql.write_pods() != [f"pod/{mysql.pod(primary)}"]:
            return primary, self.fail("write_service_not_on_primary", primary=primary)
        status = mysql.replication(replica)
        if status != {"source": mysql.server_uuid(primary), "io": "ON", "sql": "ON"}:
            return primary, self.fail("replica_not_replicating", replica=replica, status=status)
        if not mysql.applies_everything(replica):
            return primary, self.fail("replica_skips_changes", replica=replica)
        if not mysql.consistent(replica, primary):
            return primary, self.fail("regions_inconsistent", replica=replica)
        if self.problem.app.issue_counters_behind(primary):
            return primary, self.fail("issue_counter_behind")
        if self.problem.app.issue_statistics_stale(primary):
            return primary, self.fail("issue_statistics_stale")
        return primary, None

    def check_divergence(self, primary):
        problem, app = self.problem, self.problem.app
        for region in REGIONS:
            report = divergence_report(
                problem.shared["issues"],
                problem.orphaned,
                problem.accepted_after_promotion,
                app.snapshot(region)["issues"],
                keys=gitea_keys(problem.repository_ids),
            )
            if failed := self.verdict(report):
                return {**failed, "member": region}
        events, lost = issue_events(
            app.snapshot(primary),
            [*problem.shared_receipts, *problem.orphaned, *problem.accepted_after_promotion],
            problem.updates,
        )
        if lost:
            return self.fail("acknowledged_updates_lost", updates=lost)
        app.wait_for_webhook_attempts(primary)
        relay = app.wait_for_relay()
        if relay["paused"] or relay["queued"]:
            return self.fail("webhook_backlog_not_drained", paused=relay["paused"], queued=len(relay["queued"]))
        deliveries = delivery_report(app.webhook_deliveries(), events)
        if deliveries["conflicting"]:
            return self.fail("webhook_events_conflicting", issues=deliveries["conflicting"])
        if deliveries["duplicated"]:
            return self.fail("webhook_events_duplicated", events=deliveries["duplicated"])
        if deliveries["undelivered"]:
            return self.fail("webhook_events_undelivered", events=deliveries["undelivered"])
        if failed := self.check_search(primary):
            return failed
        git = app.git_inventory()
        for repository, files in problem.expected_git.items():
            if any(git.get(repository, {}).get(path) != digest for path, digest in files.items()):
                return self.fail("acknowledged_git_data_changed", repository=repository)
        return None

    def evaluate(self):
        problem, app = self.problem, self.problem.app
        if not self.baseline or self.volumes is None:
            return self.fail("baseline_not_captured")
        # Sampled, then paused while grading so checksums see a still primary; the pause is not an outage.
        traffic = None
        try:
            if problem.live_users is not None:
                traffic = app.live_traffic()
                app.live_traffic("pause")
            verdict = self.grade(traffic)
        except Exception as exc:
            verdict = self.fail_from_exception(exc)
        if traffic is not None:
            try:
                app.live_traffic("resume")
                self.graded = [*self.graded, (traffic["now"], app.live_traffic()["now"])]
            except Exception as exc:
                return self.fail_from_exception(exc)
        return verdict

    def grade(self, traffic):
        problem, app = self.problem, self.problem.app
        endpoints = ServiceEndpointMitigationOracle.evaluate(self)
        if not endpoints.get("success"):
            return endpoints
        try:
            if failed := self.check_resources():
                return failed
            app.check_workflow(token=secrets.token_hex(16))
            primary, failed = self.check_replication()
            if failed:
                return failed
            app.run_client("verify", **self.baseline, fixture=json.loads((FIXTURES / "import-data.json").read_text()))
            if problem.shared is not None and (failed := self.check_divergence(primary)):
                return failed
            if traffic is not None and (failed := self.check_live_traffic(primary, traffic)):
                return failed
        except Exception as exc:
            return self.fail_from_exception(exc)
        return {
            "success": True,
            "scale_tier": app.scale_tier,
            "orphaned_writes_recovered": len(problem.orphaned),
            "post_promotion_writes_retained": len(problem.accepted_after_promotion),
        }
