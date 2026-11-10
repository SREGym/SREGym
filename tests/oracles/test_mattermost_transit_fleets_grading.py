"""Tier 2 grading: chat served and notifications delivered, both, in every phase.

Two fleets share the provider network and its instance quota, so a repair that
restores one by starving the other fails. Notifications are judged per direct
message: a message counts once it is old enough to judge, and is delivered when
the push receiver saw it within a minute.
"""

from types import SimpleNamespace

import pytest

from sregym.conductor.oracles.apps.mattermost import transit_fleets_recovery as grading
from sregym.conductor.oracles.apps.mattermost.transit_fleets_recovery import (
    MattermostTransitFleetsOracle,
    fleets_verdict,
    push_delivery,
)
from sregym.conductor.oracles.apps.mattermost.transit_recovery import MattermostTransitOracle
from sregym.conductor.oracles.failure import FailureClass
from sregym.conductor.oracles.postgres_application import PostgresApplicationOracle


def post(at, post_id):
    return {"at": at, "post_id": post_id}


def delivery(at, post_id):
    return {"at": at, "ack_id": "ack-" + post_id, "post_id": post_id}


# ---- push delivery -----------------------------------------------------


def test_messages_too_recent_to_judge_are_left_out():
    posts = [post(100, "a"), post(150, "b")]
    push = push_delivery(posts, [delivery(101, "a")], until=200, within=60)
    assert push == {"judged": 1, "on_time": 1, "late": 0, "missing": 0}


def test_a_delivery_after_the_minute_is_late_and_none_is_missing():
    posts = [post(0, "a"), post(10, "b"), post(20, "c")]
    push = push_delivery(posts, [delivery(5, "a"), delivery(90, "b")], until=200, within=60)
    assert push == {"judged": 3, "on_time": 1, "late": 1, "missing": 1}


def test_a_delivery_seen_before_the_post_returned_is_on_time():
    push = push_delivery([post(10.4, "a")], [delivery(10.1, "a")], until=100, within=60)
    assert push["on_time"] == 1


def test_only_the_first_delivery_of_a_post_counts():
    deliveries = [delivery(100, "a"), delivery(1, "a")]
    assert push_delivery([post(0, "a")], deliveries, until=100, within=60)["on_time"] == 1


def test_deliveries_for_other_posts_do_not_count():
    push = push_delivery([post(0, "a")], [delivery(1, "zz"), {"at": 1, "post_id": None}], until=100, within=60)
    assert push["missing"] == 1


# ---- verdict -----------------------------------------------------------


def customers(sent=1000, served=900, messages_sent=50, messages_posted=48, latency_ms=50.0):
    return {
        "sent": sent,
        "served": served,
        "shed": sent - served,
        "failed": 0,
        "abandoned": 0,
        "latency_ms_total": served * latency_ms,
        "messages_sent": messages_sent,
        "messages_posted": messages_posted,
    }


def push(judged=40, on_time=39, late=0):
    return {"judged": judged, "on_time": on_time, "late": late, "missing": judged - on_time - late}


def verdict(phase="degraded", chat=None, notifications=None, forwarded=900, budget_ms=None):
    return fleets_verdict(
        phase,
        customers() if chat is None else chat,
        push() if notifications is None else notifications,
        forwarded,
        chat_required=0.8,
        push_required=0.9,
        budget_ms=budget_ms,
    )


def test_both_fleets_serving_passes():
    reason, detail = verdict()
    assert reason is None
    assert detail["served_ratio"] == 0.9 and detail["push_delivered_ratio"] == 0.975


def test_routing_around_the_provider_network_fails():
    assert verdict(forwarded=10)[0] == "provider_network_bypassed"


@pytest.mark.parametrize(
    "phase,suffix",
    [("degraded", "while_degraded"), ("recovered", "after_recovery"), ("instance_loss", "after_instance_loss")],
)
def test_each_phase_names_its_own_reasons(phase, suffix):
    assert verdict(phase, chat=customers(served=500), forwarded=500)[0] == "chat_not_served_" + suffix
    assert verdict(phase, notifications=push(on_time=20))[0] == "pushes_not_delivered_" + suffix


def test_direct_messages_posted_may_fall_a_little_below_the_requests_served():
    reason, detail = verdict(chat=customers(messages_posted=39))
    assert reason is None and detail["required_messages_posted_ratio"] == pytest.approx(0.77)
    assert verdict(chat=customers(messages_posted=38))[0] == "chat_not_served_while_degraded"


def test_direct_messages_refused_fail_chat_even_when_reads_are_served():
    reason, detail = verdict(chat=customers(served=950, messages_posted=10), forwarded=950)
    assert reason == "chat_not_served_while_degraded" and detail["messages_posted_ratio"] == 0.2


def test_chat_restored_by_starving_notifications_fails():
    assert verdict(chat=customers(served=990), forwarded=990, notifications=push(on_time=12))[0] == (
        "pushes_not_delivered_while_degraded"
    )


def test_notifications_switched_off_fail_on_their_own_reason():
    assert verdict("recovered", notifications=push(on_time=0))[0] == "push_delivery_suppressed"


def test_late_notifications_are_not_suppressed_but_still_fail():
    assert verdict(notifications=push(on_time=0, late=40))[0] == "pushes_not_delivered_while_degraded"


def test_no_message_old_enough_to_judge_is_an_environment_problem():
    with pytest.raises(RuntimeError):
        verdict(notifications=push(judged=0, on_time=0))


def test_no_message_sent_is_an_environment_problem():
    with pytest.raises(RuntimeError):
        verdict(chat=customers(messages_sent=0, messages_posted=0), notifications=push(judged=0, on_time=0))


def test_no_message_posted_at_all_fails_chat_rather_than_the_environment():
    refused = customers(served=0, messages_posted=0)
    reason, detail = verdict(chat=refused, notifications=push(judged=0, on_time=0), forwarded=0)
    assert reason == "chat_not_served_while_degraded" and detail["push_delivered_ratio"] is None


def test_latency_is_judged_only_where_a_budget_is_given():
    slow = customers(served=995, latency_ms=900.0)
    assert verdict("degraded", chat=slow, forwarded=995)[0] is None
    assert verdict("recovered", chat=slow, forwarded=995, budget_ms=500.0)[0] == "gateway_latency_unresolved"


def test_every_reason_is_an_agent_error():
    classes = MattermostTransitFleetsOracle.FAILURE_CLASSES
    for reason in (
        "push_relay_missing",
        "push_delivery_suppressed",
        "chat_not_served_while_degraded",
        "pushes_not_delivered_after_recovery",
        "chat_not_served_after_instance_loss",
        "pushes_not_delivered_after_instance_loss",
    ):
        assert classes[reason] is FailureClass.AGENT_ERROR
    assert "gateway_capacity_below_floor" not in classes


def test_required_chat_ratio_leaves_the_push_relay_one_place_of_the_quota():
    app = SimpleNamespace(
        fleet_max=20, instance_quota=20, gateway_workers=8, transit_impaired_latency=1.2, surge_rate=140
    )
    oracle = MattermostTransitFleetsOracle(SimpleNamespace(app=app, namespace="mattermost", kubectl=None))
    assert oracle.required_degraded_ratio() == pytest.approx(0.966 * 16 * 5.2 / 140)


def test_tier_2_grades_with_the_fleets_oracle():
    from sregym.conductor.problems.apps.mattermost import transit_saturation

    assert transit_saturation.mitigation_oracle_class("2") is MattermostTransitFleetsOracle
    assert transit_saturation.mitigation_oracle_class("1b") is MattermostTransitOracle


# ---- evaluate ----------------------------------------------------------


class FleetsWorld:
    scaler_interval = 15
    scaler_stabilization_seconds = 45
    transit_capacity = 80
    transit_capacity_fixed = 1000
    transit_impaired_latency = 1.2
    fleet_max = 20
    instance_quota = 20
    gateway_workers = 8
    surge_rate = 140
    healthy_rate = 60
    message_rate = 5
    states_capacity_requirement = False

    def __init__(self, world):
        self.world = world
        self.actions = []
        self.phase = "degraded"
        self.clock = 1000.0
        self.counters = {key: 0 for key in grading.CUSTOMER_COUNTERS}
        self.forwarded = 0
        self.posts, self.deliveries = [], []
        self.ticks = 0
        self.wave = 0

    def ratio(self, name, default):
        return self.world.get(f"{self.phase}_{self.wave}_{name}", self.world.get(f"{self.phase}_{name}", default))

    def command(self, *args, **kwargs):
        self.actions.append(args)
        if args[0] == "scale":
            self.phase = "instance_loss"
        if args[:2] == ("get", "deployment"):
            return "" if args[2] in self.world.get("missing", ()) else f"deployment.apps/{args[2]}"
        return ""

    def set_transit_capacity(self, value):
        self.actions.append(("transit", value))
        if value == self.transit_capacity_fixed:
            self.phase = "recovered"

    def set_customer_rate(self, value):
        self.actions.append(("customers", value))
        if self.phase in ("degraded", "lull"):
            self.phase = "degraded" if value == self.surge_rate else "lull"
            self.wave += value == self.surge_rate

    def set_message_rate(self, value):
        self.actions.append(("messages", value))

    def tick(self):
        """Five seconds of traffic: reads, and a direct message every second."""
        self.clock += 5
        self.ticks += 1
        ratio = self.ratio("served", 0.9 if self.phase == "degraded" else 0.99)
        served = int(50 * ratio)
        self.counters["sent"] += 50
        self.counters["served"] += served
        self.counters["latency_ms_total"] += served * 20
        self.counters["messages_sent"] += 5
        self.counters["messages_posted"] += 5
        self.forwarded += served
        pushed = self.ratio("pushed", 1.0)
        for n in range(5):
            post_id = f"p{len(self.posts)}"
            self.posts.append(post(self.clock - 5 + n, post_id))
            if n < round(5 * pushed):
                self.deliveries.append(delivery(self.clock - 5 + n + 2, post_id))

    def customer_stats(self):
        return {"at": self.clock, **self.counters}

    def transit_stats(self):
        return {"forwarded": self.forwarded}

    def push_records(self, since):
        return [p for p in self.posts if p["at"] >= since], [d for d in self.deliveries if d["at"] >= since]

    def fleet(self, group):
        if group == "chat-gateway":
            self.tick()
        return {"requested": 3, "in_service": 3, "provisioning": 0}

    def provisioner_errors(self, since_seconds):
        return 0

    def scaler_decisions(self):
        return []


@pytest.fixture
def run(monkeypatch):
    monkeypatch.setattr(grading.time, "sleep", lambda seconds: None)
    clock = [0.0]

    def monotonic():
        clock[0] += 5
        return clock[0]

    monkeypatch.setattr(grading.time, "monotonic", monotonic)
    monkeypatch.setattr(PostgresApplicationOracle, "evaluate", lambda self: {"success": True})

    def run(**world):
        app = FleetsWorld(world)
        oracle = MattermostTransitFleetsOracle(SimpleNamespace(app=app, namespace="mattermost", kubectl=None))
        oracle.baseline, oracle.baseline_latency_ms = {"token": "ab"}, 20.0
        return oracle.evaluate(), app

    return run


def test_a_repair_that_serves_both_fleets_passes_every_phase(run):
    result, app = run()
    assert result["success"], result
    assert {"degraded", "degraded_2", "recovered", "instance_loss"} <= set(result)
    assert result["degraded"]["push"]["judged"] > 0 and result["degraded_2"]["push"]["judged"] > 0
    for lull in (result["lull"], result["lull_2"]):
        assert lull["smallest"] == {"chat-gateway": 3, "push-proxy": 3} and lull["decisions"] == []
        assert 6 <= len(lull["fleet"]) <= 8 and lull["fleet"][-1]["t"] >= grading.LULL_SECONDS
    order = [a for a in app.actions if a[0] in ("transit", "scale", "customers", "messages")]
    assert order == [
        ("transit", 80),
        ("customers", 60),
        ("customers", 140),
        ("customers", 60),
        ("customers", 140),
        ("scale", "deployment/chat-gateway", "--replicas=1"),
        ("scale", "deployment/push-proxy", "--replicas=1"),
        ("transit", 1000),
        ("customers", 60),
    ]


def test_instance_loss_is_judged_while_the_network_is_still_degraded(run):
    result, _ = run()
    loss = result["instance_loss"]
    assert loss["required_served_ratio"] == result["degraded"]["required_served_ratio"]
    assert loss["required_push_ratio"] == result["degraded"]["required_push_ratio"]


@pytest.mark.parametrize(
    "world,reason,phase",
    [
        ({"degraded_pushed": 0.4}, "pushes_not_delivered_while_degraded", "degraded"),
        ({"recovered_pushed": 0.0}, "push_delivery_suppressed", "recovered"),
        ({"recovered_served": 0.5}, "chat_not_served_after_recovery", "recovered"),
        ({"instance_loss_pushed": 0.6}, "pushes_not_delivered_after_instance_loss", "instance_loss"),
        ({"instance_loss_served": 0.5}, "chat_not_served_after_instance_loss", "instance_loss"),
    ],
)
def test_each_phase_fails_on_its_own_evidence(run, world, reason, phase):
    result, _ = run(**world)
    assert result["success"] is False and result["reason"] == reason
    assert result["detail"]["phase"] == phase


def test_only_the_wave_after_the_lull_is_judged(run):
    result, _ = run(lull_served=0.1, lull_pushed=0.0)
    assert result["success"], result
    assert result["degraded"]["served_ratio"] == 0.9


def test_a_wave_served_short_after_the_lull_fails_while_degraded(run):
    result, _ = run(degraded_served=0.5)
    assert result["reason"] == "chat_not_served_while_degraded"


def test_the_second_wave_is_judged_on_its_own(run):
    result, app = run(degraded_2_served=0.5)
    assert result["reason"] == "chat_not_served_while_degraded"
    assert result["detail"]["phase"] == "degraded_2"
    assert result["detail"]["degraded"]["served_ratio"] == 0.9
    assert not [a for a in app.actions if a[0] == "scale"]


def test_a_failed_application_check_keeps_the_phases(run, monkeypatch):
    monkeypatch.setattr(PostgresApplicationOracle, "evaluate", lambda self: {"success": False, "reason": "x"})
    result, _ = run()
    assert result["reason"] == "x" and {"degraded", "recovered", "instance_loss"} <= set(result)


def test_tier_2_probes_the_edge_port():
    from sregym.conductor.problems.apps.mattermost.transit_saturation import MattermostTransitSaturationProblem

    assert MattermostTransitSaturationProblem("2").expected_service_port == 8443
    assert MattermostTransitSaturationProblem("1b").expected_service_port == 8080


def test_a_deleted_push_relay_fails_before_any_traffic(run):
    result, app = run(missing=("push-proxy",))
    assert result["reason"] == "push_relay_missing"
    assert not [a for a in app.actions if a[0] == "transit"]
