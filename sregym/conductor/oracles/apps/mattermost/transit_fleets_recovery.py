"""Grade tier 2 of the transit cascade: chat served and notifications delivered.

Two fleets -- the chat edge and the push relay -- cross the same provider
network and share one provider instance quota, so a repair can restore one by
starving the other. Each phase therefore judges both: the share of customer
requests and direct messages served through the edge, and the share of direct
messages whose notification reached the push service within a minute. No
phase counts instances.

The phases: the provider network still degraded, where traffic twice eases
off and surges again, and each surge is judged; then, still
degraded, both fleets cut to one instance, which only working capacity
automation recovers from; then the network restored under normal traffic.
Instance loss comes before the restoration because afterwards one instance
serves normal traffic.
"""

import time

from sregym.conductor.oracles.apps.mattermost.transit_recovery import (
    DEGRADED_SERVICE_FRACTION,
    MIN_TRANSIT_SHARE,
    RECOVERED_SERVED_RATIO,
    MattermostTransitOracle,
    delta,
    latency_budget,
    mean_latency_ms,
    served_ratio,
)
from sregym.conductor.oracles.failure import FailureClass
from sregym.conductor.oracles.postgres_application import PostgresApplicationOracle

CUSTOMER_COUNTERS = (
    "sent",
    "served",
    "shed",
    "failed",
    "abandoned",
    "latency_ms_total",
    "messages_sent",
    "messages_posted",
)
FLEETS = ("chat-gateway", "push-proxy")
#: A notification counts as delivered when it reached the push service this
#: long after its message was posted.
PUSH_WITHIN_SECONDS = 60
#: Share of judged messages whose notification must arrive on time once the
#: network has capacity again, and after instance loss.
RECOVERED_PUSH_RATIO = 0.95
#: Requests a second one nginx instance serves over the impaired network,
#: measured on hp073 after the grace period (t2cal3: 5.19-5.35 with 16-18 in
#: service), not the 6.7 its connection limit allows.
EDGE_IMPAIRED_THROUGHPUT = 5.2
#: Places under the quota the push relay needs to deliver over the impaired
#: network, with one to spare: on hp073 three relays delivered 0.93 of
#: notifications on time and two 0.70.
PUSH_PLACES = 4
#: Fraction of what the chat edge could serve over the impaired network with
#: every place under the quota but the push relay's: 0.574 of requests, midway
#: between right repairs' worse wave (0.589 and up) and wrong ones' (0.559 and
#: below) on hp073 (t2cal22).
DEGRADED_CHAT_FRACTION = 0.966
#: Direct messages posted may fall this far below the share of requests served:
#: the same repair posted 0.569-0.594 of them across runs on hp073 (t2cal21)
#: while serving 0.589-0.590 of requests.
MESSAGES_POSTED_MARGIN = 0.03
#: Between waves the network stays degraded and traffic eases to this share of
#: normal. At the surge a CPU scale-in policy only takes back instances that
#: never got a place under the quota, since saturation adds back whatever it
#: removes; in the lull workers are no longer saturated, and it removes
#: instances in service.
LULL_RATE_FRACTION = 1.0
#: Long enough for several scale-in decisions, each after its stabilization.
LULL_SECONDS = 180
#: The next wave, judged from its first second: capacity removed in the lull has
#: to boot and register again while it is under way.
DEGRADED_WINDOW_SECONDS = 300
#: Lulls and waves while degraded, each wave judged. Under a CPU scale-in
#: policy the fleets swing between scaling in and out through a lull; a wave
#: that arrives just after they swung back up costs little (t2cal21: 0.56-0.57
#: against 0.525-0.53 otherwise).
DEGRADED_WAVES = 2
#: Instance loss while the network is still degraded: time for the scale-out
#: policy to grow each fleet back from one (four instances a minute), then
#: the window judged.
LOSS_RECOVERY_SECONDS = 360
LOSS_WINDOW_SECONDS = 300
SUFFIXES = {"degraded": "while_degraded", "recovered": "after_recovery", "instance_loss": "after_instance_loss"}


def push_delivery(posts, deliveries, until, within=PUSH_WITHIN_SECONDS):
    """Messages old enough to judge at ``until``, by how their notification arrived."""
    first = {}
    for record in deliveries:
        post_id = record.get("post_id")
        if post_id:
            first[post_id] = min(record["at"], first.get(post_id, record["at"]))
    judged = [p for p in posts if p["at"] <= until - within]
    on_time = sum(1 for p in judged if p["post_id"] in first and first[p["post_id"]] - p["at"] <= within)
    late = sum(1 for p in judged if p["post_id"] in first) - on_time
    return {"judged": len(judged), "on_time": on_time, "late": late, "missing": len(judged) - on_time - late}


def fleets_verdict(phase, customers, push, forwarded, chat_required, push_required, budget_ms=None):
    """One phase of tier 2. Returns ``(reason, detail)``.

    ``budget_ms`` is the chat latency budget, given only once the network has
    capacity again.
    """
    if not customers["messages_sent"]:
        raise RuntimeError(f"No direct message was sent in the window: {customers}")
    posted = customers["messages_posted"] / customers["messages_sent"]
    posted_required = chat_required - MESSAGES_POSTED_MARGIN
    delivered = push["on_time"] / push["judged"] if push["judged"] else None
    latency = mean_latency_ms(customers)
    detail = {
        "served_ratio": round(served_ratio(customers), 3),
        "messages_posted_ratio": round(posted, 3),
        "required_served_ratio": round(chat_required, 3),
        "required_messages_posted_ratio": round(posted_required, 3),
        "push_delivered_ratio": None if delivered is None else round(delivered, 3),
        "required_push_ratio": round(push_required, 3),
        "mean_latency_ms": None if latency is None else round(latency, 1),
        "customers": customers,
        "push": push,
        "transit_forwarded": forwarded,
    }
    suffix = SUFFIXES[phase]
    if customers["served"] and forwarded < MIN_TRANSIT_SHARE * customers["served"]:
        return "provider_network_bypassed", detail
    if served_ratio(customers) < chat_required or posted < posted_required:
        return "chat_not_served_" + suffix, detail
    if delivered is None:
        # Messages were posted, so some must be old enough by the window's end.
        raise RuntimeError(f"No direct message was old enough to judge its notification: {push}")
    if push["on_time"] + push["late"] == 0:
        return "push_delivery_suppressed", detail
    if delivered < push_required:
        return "pushes_not_delivered_" + suffix, detail
    if budget_ms is not None:
        detail["latency_budget_ms"] = round(budget_ms, 1)
        if latency is None or latency > budget_ms:
            return "gateway_latency_unresolved", detail
    return None, detail


class MattermostTransitFleetsOracle(MattermostTransitOracle):
    FAILURE_CLASSES = {
        reason: FailureClass.AGENT_ERROR
        for reason in (
            "gateway_missing",
            "push_relay_missing",
            "provider_network_bypassed",
            "push_delivery_suppressed",
            "gateway_latency_unresolved",
            *(
                f"{kind}_{suffix}"
                for kind in ("chat_not_served", "pushes_not_delivered")
                for suffix in SUFFIXES.values()
            ),
        )
    }

    def capture_baseline(self):
        super().capture_baseline()
        app = self.problem.app
        now = app.customer_stats()["at"]
        posts, deliveries = app.push_records(since=now - 3 * PUSH_WITHIN_SECONDS)
        push = push_delivery(posts, deliveries, until=now)
        if not push["judged"] or push["on_time"] / push["judged"] < RECOVERED_PUSH_RATIO:
            raise RuntimeError(f"Notifications are not delivered before the incident: {push}")

    def watch(self, seconds):
        """Both fleets, customer and transit counters, and notifications, across a window.

        The window runs a minute longer than asked so that every message posted
        in the part asked for can be judged.
        """
        app = self.problem.app
        customers_before, transit_before = app.customer_stats(), app.transit_stats()
        samples, started = [], time.monotonic()
        while True:
            elapsed = time.monotonic() - started
            samples.append({"t": round(elapsed, 1), **{group: app.fleet(group) for group in FLEETS}})
            if elapsed >= seconds + PUSH_WITHIN_SECONDS:
                break
            time.sleep(self.sample_seconds)
        customers_after = app.customer_stats()
        posts, deliveries = app.push_records(since=customers_before["at"] - PUSH_WITHIN_SECONDS)
        posts = [p for p in posts if p["at"] >= customers_before["at"]]
        push = push_delivery(posts, deliveries, until=customers_after["at"])
        customers = delta(customers_before, customers_after, CUSTOMER_COUNTERS)
        forwarded = app.transit_stats()["forwarded"] - transit_before["forwarded"]
        return samples, customers, push, forwarded

    def lull(self):
        """Both fleets through the lull, recorded but not judged: one sample about
        every half minute, and the capacity decisions taken during it."""
        app = self.problem.app
        samples, started, since = [], time.monotonic(), time.time()
        while True:
            elapsed = time.monotonic() - started
            samples.append({"t": round(elapsed, 1), **{group: app.fleet(group) for group in FLEETS}})
            if elapsed >= LULL_SECONDS:
                every = max(1, round(30 / self.sample_seconds))
                return {
                    "fleet": [*samples[:-1:every], samples[-1]],
                    "smallest": {g: min(s[g]["in_service"] for s in samples) for g in FLEETS},
                    "decisions": [
                        f"{d.get('group')}: {d.get('activity')}"
                        for d in app.scaler_decisions()
                        if d.get("at", 0) >= since
                    ],
                }
            time.sleep(self.sample_seconds)

    def required_degraded_ratio(self):
        """The fleets share the quota, and the push relay needs its places of it."""
        app = self.problem.app
        instances = min(app.fleet_max, app.instance_quota - PUSH_PLACES)
        return DEGRADED_CHAT_FRACTION * min(1.0, instances * EDGE_IMPAIRED_THROUGHPUT / app.surge_rate)

    def required_degraded_push_ratio(self):
        """Each relay holds only so many sends across the impaired network at once,
        so delivery depends on how many relays hold places under the quota."""
        return DEGRADED_SERVICE_FRACTION

    def judge(self, phases, phase, seconds, chat_required, push_required, budget_ms=None, key=None):
        """Watch one phase and record its detail under ``key``; a failure verdict, or None."""
        key = key or phase
        samples, customers, push, forwarded = self.watch(seconds)
        reason, phases[key] = fleets_verdict(phase, customers, push, forwarded, chat_required, push_required, budget_ms)
        phases[key]["fleet"] = samples[-6:]
        if reason:
            return self.fail(reason, phase=key, decisions=self.problem.app.scaler_decisions()[-5:], **phases)
        return None

    def evaluate(self):
        app = self.problem.app
        if not self.baseline or self.baseline_latency_ms is None:
            return PostgresApplicationOracle.evaluate(self)
        phases = {}
        try:
            for group, reason in zip(FLEETS, ("gateway_missing", "push_relay_missing"), strict=True):
                if not app.command("get", "deployment", group, "--ignore-not-found", "-o", "name").strip():
                    return self.fail(reason)

            # Phase one: the network the responder cannot touch is still
            # degraded. Traffic eases off, then the next wave is judged; twice.
            app.set_transit_capacity(app.transit_capacity)
            for wave in range(1, DEGRADED_WAVES + 1):
                suffix = "" if wave == 1 else f"_{wave}"
                app.set_customer_rate(LULL_RATE_FRACTION * app.healthy_rate)
                phases["lull" + suffix] = self.lull()
                app.set_customer_rate(app.surge_rate)
                if failure := self.judge(
                    phases,
                    "degraded",
                    DEGRADED_WINDOW_SECONDS,
                    self.required_degraded_ratio(),
                    self.required_degraded_push_ratio(),
                    key="degraded" + suffix,
                ):
                    return failure

            # Still degraded, both fleets lose all but one instance. Once the
            # network has capacity again one instance serves normal traffic, so
            # only now does losing instances need the automation to recover.
            for group in FLEETS:
                app.command("scale", f"deployment/{group}", f"--replicas={self.challenge_replicas}")
            time.sleep(LOSS_RECOVERY_SECONDS)
            if failure := self.judge(
                phases,
                "instance_loss",
                LOSS_WINDOW_SECONDS,
                self.required_degraded_ratio(),
                self.required_degraded_push_ratio(),
            ):
                return failure

            # The provider restores capacity; traffic returns to normal.
            app.set_transit_capacity(app.transit_capacity_fixed)
            app.set_customer_rate(app.healthy_rate)
            time.sleep(self.settle_seconds)
            if failure := self.judge(
                phases,
                "recovered",
                self.window_seconds,
                RECOVERED_SERVED_RATIO,
                RECOVERED_PUSH_RATIO,
                budget_ms=latency_budget(self.baseline_latency_ms),
            ):
                return failure

            for group in FLEETS:
                app.command("rollout", "status", f"deployment/{group}", "--timeout=300s", timeout=330)
            result = PostgresApplicationOracle.evaluate(self)
            if not result.get("success"):
                return {**result, **phases}
        except Exception as exc:
            return self.fail_from_exception(exc)
        return {**result, **phases}
