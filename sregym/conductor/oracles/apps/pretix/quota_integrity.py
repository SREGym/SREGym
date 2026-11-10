"""The oversold workshop is made consistent again without breaking the shop or losing data.

White-box state checks, read straight from PostgreSQL through the pretix-web pod:

- Platform intact: every topology Deployment keeps its replica, the PVCs are the
  original ones, the event is still live and its waiting list is still enabled
  (the fix may not take the shop or the feature offline).
- The oversold quota holds no more valid tickets than its size: the count of
  PAID/PENDING order positions on the quota is at most the quota size, and a
  door check-in of every holder cannot admit two people for one seat.
- The first legitimate buyer is untouched: the order that genuinely held the last
  seat before the incident is still PAID (rejecting "cancel the oldest / cancel
  everyone").
- No bulk data loss: the other paid registrations on the event are still there.

A blind "cancel the newest order" leaves a consistent quota and keeps the first
buyer, so it passes here by design -- distinguishing it from the real mechanism
is the diagnosis judge's job, not this oracle's.
"""

import json

from sregym.conductor.oracles.base import Oracle
from sregym.conductor.oracles.failure import FailureClass


class PretixQuotaIntegrityOracle(Oracle):
    evaluation_timeout_seconds = 600
    readiness_grace_seconds = 300

    FAILURE_CLASSES = {
        "required_application_topology_changed": FailureClass.AGENT_ERROR,
        "deployment_not_ready": FailureClass.AGENT_ERROR,
        "persistent_volumes_replaced_or_missing": FailureClass.AGENT_ERROR,
        "event_not_live": FailureClass.AGENT_ERROR,
        "waiting_list_disabled": FailureClass.AGENT_ERROR,
        "quota_still_oversold": FailureClass.AGENT_ERROR,
        "two_valid_tickets_for_one_seat": FailureClass.AGENT_ERROR,
        "first_buyer_ticket_lost": FailureClass.AGENT_ERROR,
        "registrations_deleted": FailureClass.AGENT_ERROR,
        "baseline_not_captured": FailureClass.AMBIGUOUS,
        "probe_failed": FailureClass.AMBIGUOUS,
    }

    def __init__(self, problem):
        super().__init__(problem)
        self.baseline = None

    @property
    def app(self):
        return self.problem.app

    def capture_baseline(self):
        self.baseline = {
            "volumes": self._volume_ids(),
            "paid_positions": self._event_paid_positions(),
        }

    def _volume_ids(self):
        claims = json.loads(self.app.command("get", "pvc", "-o", "json"))["items"]
        return {p["metadata"]["name"]: p["metadata"]["uid"] for p in claims}

    def _event_paid_positions(self):
        return int(
            self.app.pretix_python(
                PROBE,
                {"organizer": self.problem.ORGANIZER, "event": self.problem.EVENT, "mode": "event_paid_positions"},
            )["value"]
        )

    def _probe(self, mode, **kw):
        return self.app.pretix_python(
            PROBE, {"organizer": self.problem.ORGANIZER, "event": self.problem.EVENT, "mode": mode, **kw}
        )

    def evaluate(self) -> dict:
        if not self.baseline:
            return self.fail("baseline_not_captured")
        app = self.app
        try:
            # 1. Platform intact
            for name, replicas in app.topology.items():
                found = app.command("get", "deployment", name, "--ignore-not-found", "-o", "json").strip()
                if not found:
                    return self.fail("required_application_topology_changed", deployment=name, missing=True)
                spec = json.loads(found)["spec"].get("replicas", 1)
                if spec != replicas:
                    return self.fail(
                        "required_application_topology_changed", deployment=name, replicas=spec, expected=replicas
                    )
                ready = json.loads(found).get("status", {}).get("readyReplicas", 0)
                if ready != replicas:
                    return self.fail("deployment_not_ready", deployment=name, ready=ready, expected=replicas)
            now_vols = self._volume_ids()
            for name, uid in self.baseline["volumes"].items():
                if now_vols.get(name) != uid:
                    return self.fail("persistent_volumes_replaced_or_missing", volume=name)

            # 2. Event and feature still on
            state = self._probe("event_state")
            if not state["live"]:
                return self.fail("event_not_live")
            if not state["waiting_list_enabled"]:
                return self.fail("waiting_list_disabled")

            # 3. The quota is no longer oversold and no seat is double-admitted
            quota = self._probe("quota_integrity", quota=self.problem.target_quota)
            if quota["valid_tickets"] > quota["size"]:
                return self.fail(
                    "quota_still_oversold",
                    quota=self.problem.target_quota,
                    valid_tickets=quota["valid_tickets"],
                    size=quota["size"],
                )
            if quota["max_admitted_per_seat"] > 1 or quota["admitted"] > quota["size"]:
                return self.fail(
                    "two_valid_tickets_for_one_seat",
                    quota=self.problem.target_quota,
                    admitted=quota["admitted"],
                    size=quota["size"],
                )

            # 4. The first legitimate buyer keeps a valid ticket (only after an incident set one)
            first = {"status": None}
            if self.problem.first_order:
                first = self._probe("order_status", order=self.problem.first_order)
                if first["status"] not in ("p", "n") or not first["has_valid_position"]:
                    return self.fail("first_buyer_ticket_lost", order=self.problem.first_order, status=first["status"])

            # 5. No bulk deletion of the event's registrations (one cancellation is expected)
            now_paid = self._event_paid_positions()
            if now_paid < self.baseline["paid_positions"] - 2:
                return self.fail("registrations_deleted", before=self.baseline["paid_positions"], after=now_paid)
        except Exception as exc:
            return self.fail_from_exception(exc)
        return {
            "success": True,
            "quota_valid_tickets": quota["valid_tickets"],
            "quota_size": quota["size"],
            "first_order_status": first["status"],
            "event_paid_positions": now_paid,
        }


PROBE = r"""
from django_scopes import scopes_disabled, scope
from pretix.base.models import Event, Order, OrderPosition, Quota, Checkin
from django.utils.timezone import now
mode = ARGS["mode"]
with scopes_disabled():
    ev = Event.objects.select_related("organizer").get(organizer__slug=ARGS["organizer"], slug=ARGS["event"])
with scope(organizer=ev.organizer):
    if mode == "event_state":
        print(json.dumps({"live": ev.live, "waiting_list_enabled": bool(ev.settings.waiting_list_enabled)}))
    elif mode == "event_paid_positions":
        n = OrderPosition.objects.filter(order__event=ev, order__status=Order.STATUS_PAID).count()
        print(json.dumps({"value": n}))
    elif mode == "order_status":
        o = Order.objects.filter(event=ev, code=ARGS["order"]).first()
        if not o:
            print(json.dumps({"status": None, "has_valid_position": False}))
        else:
            valid = OrderPosition.objects.filter(order=o).exists() and o.status in (Order.STATUS_PAID, Order.STATUS_PENDING)
            print(json.dumps({"status": o.status, "has_valid_position": valid}))
    elif mode == "quota_integrity":
        q = Quota.objects.get(pk=ARGS["quota"])
        items = list(q.items.values_list("pk", flat=True))
        positions = OrderPosition.objects.filter(
            order__event=ev, item_id__in=items,
            order__status__in=[Order.STATUS_PAID, Order.STATUS_PENDING],
        )
        valid_tickets = positions.count()
        # admitted (checked in) per position, and the max times any single seat/position was admitted
        admitted_positions = 0
        max_per_seat = 0
        for p in positions.select_related("seat"):
            cnt = Checkin.objects.filter(position=p, type=Checkin.TYPE_ENTRY).count()
            if cnt:
                admitted_positions += 1
            max_per_seat = max(max_per_seat, cnt)
        print(json.dumps({
            "size": q.size, "valid_tickets": valid_tickets,
            "admitted": admitted_positions, "max_admitted_per_seat": max_per_seat,
        }))
    else:
        raise SystemExit("unknown mode")
"""
