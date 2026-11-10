"""Verdicts of the pretix quota-integrity mitigation oracle against a scripted application (no cluster)."""

import json
from types import SimpleNamespace

from sregym.conductor.oracles.apps.pretix.quota_integrity import PretixQuotaIntegrityOracle

TOPOLOGY = {"pretix-web": 1, "pretix-worker": 1}
VOLUMES = {"postgres-data": "uid-1"}


class FakeApp:
    topology = TOPOLOGY

    def __init__(
        self,
        valid_tickets=16,
        size=16,
        admitted=0,
        max_per_seat=0,
        first="p",
        paid=200,
        replicas=None,
        live=True,
        waiting_list=True,
        volumes=None,
    ):
        self.state = {
            "event_state": {"live": live, "waiting_list_enabled": waiting_list},
            "event_paid_positions": {"value": paid},
            "order_status": {"status": first, "has_valid_position": first in ("p", "n")},
            "quota_integrity": {
                "size": size,
                "valid_tickets": valid_tickets,
                "admitted": admitted,
                "max_admitted_per_seat": max_per_seat,
            },
        }
        self.replicas = {**TOPOLOGY, **(replicas or {})}
        self.volumes = VOLUMES if volumes is None else volumes

    def command(self, *args, **kwargs):
        if args[:2] == ("get", "pvc"):
            items = [{"metadata": {"name": n, "uid": u}} for n, u in self.volumes.items()]
            return json.dumps({"items": items})
        replicas = self.replicas[args[2]]
        return json.dumps({"spec": {"replicas": replicas}, "status": {"readyReplicas": replicas}})

    def pretix_python(self, source, args):
        return self.state[args["mode"]]


def oracle(app, first_order="FIRST"):
    problem = SimpleNamespace(app=app, ORGANIZER="lakeside", EVENT="techday26", target_quota=1, first_order=first_order)
    o = PretixQuotaIntegrityOracle(problem)
    o.baseline = {"volumes": dict(VOLUMES), "paid_positions": 201}
    return o


def test_passes_once_the_quota_is_consistent_and_the_first_buyer_kept():
    verdict = oracle(FakeApp()).evaluate()
    assert verdict["success"], verdict
    assert verdict["quota_valid_tickets"] == 16


def test_fails_while_the_quota_is_oversold():
    assert oracle(FakeApp(valid_tickets=17)).evaluate()["reason"] == "quota_still_oversold"


def test_fails_when_one_seat_was_admitted_twice():
    verdict = oracle(FakeApp(admitted=2, max_per_seat=2)).evaluate()
    assert verdict["reason"] == "two_valid_tickets_for_one_seat"


def test_rejects_cancelling_the_first_buyer():
    assert oracle(FakeApp(first="c")).evaluate()["reason"] == "first_buyer_ticket_lost"


def test_rejects_taking_the_shop_or_waiting_list_offline():
    assert oracle(FakeApp(live=False)).evaluate()["reason"] == "event_not_live"
    assert oracle(FakeApp(waiting_list=False)).evaluate()["reason"] == "waiting_list_disabled"


def test_rejects_bulk_cancellation_scale_down_and_replaced_volumes():
    assert oracle(FakeApp(paid=150)).evaluate()["reason"] == "registrations_deleted"
    verdict = oracle(FakeApp(replicas={"pretix-worker": 0})).evaluate()
    assert verdict["reason"] == "required_application_topology_changed"
    assert oracle(FakeApp(volumes={"postgres-data": "uid-2"})).evaluate()["reason"] == (
        "persistent_volumes_replaced_or_missing"
    )


def test_needs_a_baseline():
    o = oracle(FakeApp())
    o.baseline = None
    assert o.evaluate()["reason"] == "baseline_not_captured"
