"""Grade recovery of the workspace after the gateway cascade.

The checks are behavioral wherever possible, so any sound repair passes:

1. users in every region reach mail and chat at the daily peak;
2. mail accepted from outside was neither bounced, lost nor duplicated, and
   the transport queue drained;
3. the gateway fleet absorbs a burst above the peak, and a single unit holds
   a few hundred long-lived connections on top of its users: both only hold
   once the defect is gone, not when load was merely shed or threads cut;
4. a unit that stops answering is routed around (automation still works);
5. losing a whole region does not take the other regions down;
6. the shared application checks: chat data, database and volumes retained.
"""

import collections
import contextlib
import re
import time

from sregym.conductor.oracles.failure import FailureClass
from sregym.conductor.oracles.postgres_application import PostgresApplicationOracle
from sregym.service.apps.workspace.application import REGIONS

PROBE_FLOOR = 0.99
DISRUPTION_FLOOR = 0.95
BURST_FACTOR = 1.6
#: Long-lived connections the load test holds on one unit per region.
UNIT_LOAD_CLIENTS = 700
UNIT_LOAD_SECONDS = 150


def unit_load_verdict(report, clients, settled_after=90):
    """Judge one unit's load-test report (edge-load's per-second lines).

    After the ramp, at most 2% of connection attempts may fail and at least
    95% of the clients must hold a connection the whole time.
    """
    ok = fail = 0
    lowest = clients
    for line in report.splitlines():
        fields = dict(re.findall(r"(\w+)=\s*(\d+)", line))
        if not {"t", "ok", "fail", "connected"} <= fields.keys() or int(fields["t"]) < settled_after:
            continue
        ok, fail = ok + int(fields["ok"]), fail + int(fields["fail"])
        lowest = min(lowest, int(fields["connected"]))
    attempts = ok + fail
    failed = fail / attempts if attempts else 1.0
    return {"held": lowest >= 0.95 * clients and failed <= 0.02, "lowest_connected": lowest, "failed_fraction": failed}


def probes_meeting(status, floor, minimum_samples, services=("mail", "chat"), regions=REGIONS):
    """Return (ok, shortfalls) for the probe windows in a client status."""
    shortfalls = {}
    for service in services:
        for region in regions:
            entry = status["probes"].get(f"{service}.{region}", {"success_ratio": 0, "samples": 0})
            if entry["samples"] < minimum_samples or entry["success_ratio"] < floor:
                shortfalls[f"{service}.{region}"] = entry
    return not shortfalls, shortfalls


def mail_verdict(accepted, delivered, bounced, in_flight=()):
    """Compare accepted message IDs with what was delivered and bounced."""
    counts = collections.Counter(delivered)
    accepted_set = set(accepted) - set(in_flight)
    return {
        "accepted": len(accepted_set),
        "bounced": sorted(set(bounced) & set(accepted)),
        "missing": sorted(m for m in accepted_set if counts[m] == 0 and m not in set(bounced)),
        "duplicated": sorted(m for m in accepted_set if counts[m] > 1),
    }


class WorkspaceRecoveryOracle(PostgresApplicationOracle):
    evaluation_timeout_seconds = 3000
    FAILURE_CLASSES = {
        "users_still_failing": FailureClass.AGENT_ERROR,
        "mail_queue_not_drained": FailureClass.AGENT_ERROR,
        "mail_bounced": FailureClass.AGENT_ERROR,
        "mail_lost": FailureClass.AGENT_ERROR,
        "mail_duplicated": FailureClass.AGENT_ERROR,
        "gateway_collapses_under_burst": FailureClass.AGENT_ERROR,
        "unit_fails_load_test": FailureClass.AGENT_ERROR,
        "failed_unit_not_routed_around": FailureClass.AGENT_ERROR,
        "region_failure_spread": FailureClass.AGENT_ERROR,
    }
    #: How long users must have been served before grading starts.
    settle_seconds = 300
    drain_seconds = 420
    burst_seconds = 240

    def wait_for_users(self, deadline_seconds, window="120s", floor=PROBE_FLOOR, **scope):
        app = self.problem.app
        deadline = time.monotonic() + deadline_seconds
        shortfalls = None
        while True:
            ok, shortfalls = probes_meeting(app.client_status(window), floor, 30, **scope)
            if ok or time.monotonic() >= deadline:
                return ok, shortfalls
            time.sleep(10)

    def isolate(self, name, selector):
        """Drop all ingress to the selected gateway pods."""
        policy = {
            "apiVersion": "networking.k8s.io/v1",
            "kind": "NetworkPolicy",
            "metadata": {"name": name, "labels": {"grading": "workspace"}},
            "spec": {"podSelector": {"matchLabels": selector}, "policyTypes": ["Ingress"], "ingress": []},
        }
        self.problem.app.apply([policy])

    def release(self):
        self.problem.app.command("delete", "networkpolicy", "-l", "grading=workspace", "--ignore-not-found")

    def disruption(self, name, selector, hold, regions, services):
        """Isolate some units, then measure the given regions over the hold."""
        app = self.problem.app
        self.isolate(name, selector)
        try:
            time.sleep(hold)
            status = app.client_status(f"{hold - 30}s")
            return probes_meeting(status, DISRUPTION_FLOOR, 10, services=services, regions=regions)
        finally:
            self.release()

    def evaluate(self):
        app = self.problem.app
        peak = app.population_for("peak")
        try:
            app.set_population(peak)
            ok, shortfalls = self.wait_for_users(self.settle_seconds)
            if not ok:
                return self.fail("users_still_failing", probes=shortfalls)

            deadline = time.monotonic() + self.drain_seconds
            while (deferred := app.deferred_count()) and time.monotonic() < deadline:
                time.sleep(15)
            mail = mail_verdict(
                app.accepted_mail(), app.delivered_message_ids(), app.bounced_mail(), self.in_flight(app)
            )
            if mail["bounced"]:
                return self.fail("mail_bounced", count=len(mail["bounced"]), sample=mail["bounced"][:5])
            if deferred:
                return self.fail("mail_queue_not_drained", deferred=deferred)
            if mail["missing"]:
                return self.fail("mail_lost", count=len(mail["missing"]), sample=mail["missing"][:5])
            if mail["duplicated"]:
                return self.fail("mail_duplicated", count=len(mail["duplicated"]), sample=mail["duplicated"][:5])

            app.set_population(app.population_for("peak", BURST_FACTOR))
            try:
                time.sleep(self.burst_seconds)
                ok, shortfalls = probes_meeting(app.client_status("120s"), PROBE_FLOOR - 0.02, 20)
            finally:
                app.set_population(peak)
            if not ok:
                return self.fail("gateway_collapses_under_burst", factor=BURST_FACTOR, probes=shortfalls)
            ok, shortfalls = self.wait_for_users(180, window="60s")
            if not ok:
                return self.fail("gateway_collapses_under_burst", phase="after burst", probes=shortfalls)

            drained = set(app.policy()["drained"].split())
            units = [next(u for u in app.units(r) if u not in drained) for r in REGIONS]
            reports = app.unit_load_test(units, UNIT_LOAD_CLIENTS, UNIT_LOAD_SECONDS)
            verdicts = {u: unit_load_verdict(r, UNIT_LOAD_CLIENTS) for u, r in reports.items()}
            if not all(v["held"] for v in verdicts.values()):
                return self.fail("unit_fails_load_test", clients=UNIT_LOAD_CLIENTS, units=verdicts)
            ok, shortfalls = self.wait_for_users(240, window="60s")
            if not ok:
                return self.fail("users_still_failing", phase="after unit load test", probes=shortfalls)

            policy = app.policy()
            on_gateway = tuple(s for s in ("mail", "chat") if policy[f"placement.{s}"] == "primary")
            if on_gateway:
                unit = app.units("a")[0]
                ok, shortfalls = self.disruption(
                    "grading-unit", {"statefulset.kubernetes.io/pod-name": unit}, 90, ("a",), on_gateway
                )
                if not ok:
                    return self.fail("failed_unit_not_routed_around", unit=unit, probes=shortfalls)
                self.wait_for_users(180, window="60s")
                ok, shortfalls = self.disruption(
                    "grading-region", {"app": "edge-gw", "region": "c"}, 120, ("a", "b"), ("mail", "chat")
                )
                if not ok:
                    return self.fail("region_failure_spread", isolated="c", probes=shortfalls)
                ok, shortfalls = self.wait_for_users(300, window="60s")
                if not ok:
                    return self.fail("users_still_failing", phase="after region loss", probes=shortfalls)
        except Exception as exc:
            return self.fail_from_exception(exc)
        finally:
            with contextlib.suppress(Exception):
                self.release()
        shared = super().evaluate()
        if not shared.get("success"):
            return shared
        return {**shared, "mail_verified": mail["accepted"], "burst_factor": BURST_FACTOR}

    @staticmethod
    def in_flight(app):
        """Messages accepted in the last minute may still be in transit."""
        cutoff = time.time() - 60
        lines = app.clients_api("/mail/accepted").splitlines()
        return [line.split()[1] for line in lines if len(line.split()) >= 2 and int(line.split()[0]) >= cutoff]
