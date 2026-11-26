"""Grade tier 2 of the transit cascade: chat served and notifications delivered.

Two fleets -- the chat edge and the push relay -- cross the same provider
network and share one provider instance quota, so a repair can restore one by
starving the other. Each phase therefore judges both: the share of customer
requests and direct messages served through the edge, and the share of direct
messages whose notification reached the push service within a minute. No
phase counts instances.

The phases: the provider network still degraded, where traffic first surges
unjudged, so that capacity the repair keeps is in place whatever size the fleets
had when grading began, then twice eases off and surges again, and each of
those surges is judged; then, still
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
#: Share of chat requests served over the impaired network that a repair must
#: reach in each judged wave and after instance loss. Each chat instance adds
#: about 0.03, so the line sits between fourteen and fifteen: a repair one place
#: short of the best split still passes. Calibrated on ms1339 with the 120 s
#: waves (t2cal25, three runs each): sixteen chat instances served 0.580-0.585,
#: fifteen 0.551-0.555, fourteen 0.522-0.526, and repairs that kept a CPU
#: scale-in policy 0.512 and below in the waves, 0.525 and below after loss.
DEGRADED_CHAT_RATIO = 0.540
#: Direct messages posted may fall this far below the share of requests served,
#: so 0.510 of them: fifteen chat instances posted 0.547-0.570 on ms1339
#: (t2cal25) while serving 0.551-0.555 of requests.
MESSAGES_POSTED_MARGIN = 0.03
#: Between waves the network stays degraded and traffic eases to this share of
#: normal. At the surge a CPU scale-in policy only takes back instances that
#: never got a place under the quota, since saturation adds back whatever it
#: removes; in the lull workers are no longer saturated, and it removes
#: instances in service. Half of normal traffic: with the fleets warmed by the
#: first surge, a lull at normal traffic left a CPU scale-in policy enough
#: instances to serve the next wave like a repair one place short (hp073,
#: t2cal23: 0.563 against 0.561).
LULL_RATE_FRACTION = 0.5
#: Long enough for several scale-in decisions, each after its stabilization.
LULL_SECONDS = 180
#: The surge before the first lull, recorded but not judged: long enough for the
#: scale-out policy to grow either fleet from its smallest size to the quota.
WARMUP_SECONDS = 300
#: The start of the next wave, judged from its first second: capacity removed in
#: the lull has to boot and register again while it is under way, and a fleet
#: that kept its capacity serves this stretch as it would the whole wave.
DEGRADED_WINDOW_SECONDS = 120
#: Lulls and waves while degraded, each wave judged. Under a CPU scale-in
#: policy the fleets swing between scaling in and out through a lull; a wave
#: that arrives just after they swung back up costs less, so the two waves of
#: one such repair differ (ms1339, t2cal25: 0.365 and 0.485, 0.384 and 0.465).
DEGRADED_WAVES = 2
#: Instance loss while the network is still degraded: time for the scale-out
#: policy to grow each fleet back from one (four instances a minute), then
#: the window judged.
LOSS_RECOVERY_SECONDS = 360
LOSS_WINDOW_SECONDS = 300
SUFFIXES = {"degraded": "while_degraded", "recovered": "after_recovery", "instance_loss": "after_instance_loss"}
#: Each fleet's rollout wait at the end of grading.
ROLLOUT_SECONDS = 330
#: Messages posted before the incident, sampled from all of them, that must still be there, unchanged, after grading.
HISTORY_POSTS = 50
HISTORY_QUERY = (
    "SELECT id || ',' || md5(message) FROM posts WHERE deleteat = 0 AND type = '' "
    f"ORDER BY random() LIMIT {HISTORY_POSTS};"
)


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


def parse_history(output):
    """``{post id: message digest}`` from psql's unaligned ``id,digest`` rows."""
    return dict(line.split(",", 1) for line in output.splitlines() if "," in line)


def history_query(post_ids):
    ids = ", ".join(f"'{post_id}'" for post_id in sorted(post_ids) if post_id.isalnum())
    return f"SELECT id || ',' || md5(message) FROM posts WHERE deleteat = 0 AND id IN ({ids});"


def history_changes(before, after):
    """Posts recorded before the incident that are now deleted or say something else."""
    return sorted(post_id for post_id, digest in before.items() if after.get(post_id) != digest)


def fleets_verdict(phase, customers, push, crossed, chat_required, push_required, budget_ms=None):
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
        "transit_requests": crossed,
    }
    suffix = SUFFIXES[phase]
    if customers["served"] and crossed < MIN_TRANSIT_SHARE * customers["served"]:
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
            "chat_history_lost",
            *(
                f"{kind}_{suffix}"
                for kind in ("chat_not_served", "pushes_not_delivered")
                for suffix in SUFFIXES.values()
            ),
        )
    }

    def __init__(self, problem):
        super().__init__(problem)
        self.history = {}

    @property
    def evaluation_timeout_seconds(self):
        """Every phase's windows, a tenth more for sampling, the rollout waits and the application check."""
        judged = PUSH_WITHIN_SECONDS
        phases = (
            WARMUP_SECONDS
            + DEGRADED_WAVES * (LULL_SECONDS + DEGRADED_WINDOW_SECONDS + judged)
            + LOSS_RECOVERY_SECONDS
            + LOSS_WINDOW_SECONDS
            + judged
            + self.settle_seconds
            + self.window_seconds
            + judged
        )
        return (
            round(1.1 * phases) + len(FLEETS) * ROLLOUT_SECONDS + PostgresApplicationOracle.evaluation_timeout_seconds
        )

    def capture_baseline(self):
        super().capture_baseline()
        app = self.problem.app
        now = app.customer_stats()["at"]
        posts, deliveries = app.push_records(since=now - 3 * PUSH_WITHIN_SECONDS)
        push = push_delivery(posts, deliveries, until=now)
        if not push["judged"] or push["on_time"] / push["judged"] < RECOVERED_PUSH_RATIO:
            raise RuntimeError(f"Notifications are not delivered before the incident: {push}")
        self.record_history()

    def record_history(self):
        """A sample of the messages already posted, to find them unchanged after grading."""
        self.history = parse_history(self.problem.app.sql(HISTORY_QUERY))
        if len(self.history) < HISTORY_POSTS:
            raise RuntimeError(f"Too few messages before the incident: {len(self.history)}")

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
        crossed = app.transit_stats()["requests"] - transit_before["requests"]
        return samples, customers, push, crossed

    def observe_unjudged(self, seconds):
        """Both fleets across an unjudged stretch: one sample about every half
        minute, the smallest each got, and the capacity decisions taken."""
        app = self.problem.app
        samples, started, since = [], time.monotonic(), time.time()
        while True:
            elapsed = time.monotonic() - started
            samples.append({"t": round(elapsed, 1), **{group: app.fleet(group) for group in FLEETS}})
            if elapsed >= seconds:
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
        return DEGRADED_CHAT_RATIO

    def required_degraded_push_ratio(self):
        """Each relay holds only so many sends across the impaired network at once,
        so delivery depends on how many relays hold places under the quota."""
        return DEGRADED_SERVICE_FRACTION

    def judge(self, phases, phase, seconds, chat_required, push_required, budget_ms=None, key=None):
        """Watch one phase and record its detail under ``key``; a failure verdict, or None."""
        key = key or phase
        samples, customers, push, crossed = self.watch(seconds)
        reason, phases[key] = fleets_verdict(phase, customers, push, crossed, chat_required, push_required, budget_ms)
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
            # degraded. A first surge lets the fleets grow to what the repair
            # allows; then traffic eases off and the next wave is judged, twice.
            app.set_transit_capacity(app.transit_capacity)
            app.set_customer_rate(app.surge_rate)
            phases["warmup"] = self.observe_unjudged(WARMUP_SECONDS)
            for wave in range(1, DEGRADED_WAVES + 1):
                suffix = "" if wave == 1 else f"_{wave}"
                app.set_customer_rate(LULL_RATE_FRACTION * app.healthy_rate)
                phases["lull" + suffix] = self.observe_unjudged(LULL_SECONDS)
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

            if lost := history_changes(self.history, parse_history(app.sql(history_query(self.history)))):
                return self.fail("chat_history_lost", posts=lost[:10], count=len(lost), **phases)

            for group in FLEETS:
                app.command("rollout", "status", f"deployment/{group}", "--timeout=300s", timeout=ROLLOUT_SECONDS)
            result = PostgresApplicationOracle.evaluate(self)
            if not result.get("success"):
                return {**result, **phases}
        except Exception as exc:
            return self.fail_from_exception(exc)
        return {**result, **phases}
