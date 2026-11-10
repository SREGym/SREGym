"""The waiting list of the wrongly closed workshop is served and the shop does not starve it again.

Pass/fail ("service restored and damage repaired"):

- Platform intact: every topology Deployment keeps its replica and is ready, the PVCs are
  the original ones, the event is live, the waiting list and its automatic assignment are
  still enabled, and no quota's size changed (raising it would overbook the room).
- Recurrence probe first: ordinary visitors open the shop and the waiting-list page (each
  computes availability counting the waiting list), then the oracle waits for the next
  per-minute cron run and visits again. A plain reopen that the next computation closes
  again is caught here.
- Then, for the target and every other workshop the same defect had closed before handover
  (ordinary shop traffic can hit it too): it is not closed while seats are free that no
  waiting customer has been offered -- free seats (size minus paid/pending tickets minus
  valid blocking vouchers not yet redeemed minus live carts) must be 0 or there must be
  nobody left waiting without a voucher. A workshop that traffic closes this way for the
  first time after handover is reported as a recurrence (``closed_after_handover``), not
  failed.
- For the target workshop: as many waiting customers as there are free seats have a valid
  offer (a waiting-list voucher, or a registration made for them), no more seats are
  offered than exist, and the waiting customers present at handover were not deleted
  without being registered.
- A served waiting customer can actually register: the oracle follows one voucher link
  through the shop and places the order.

Reported, not graded: whether close-when-sold-out is still on, whether vouchers went to
the earliest entries, and the state of every workshop. All checks run and are reported
even after an early failure.
"""

import json
import time

from sregym.conductor.oracles.base import Oracle
from sregym.conductor.oracles.failure import FailureClass
from sregym.service.apps.pretix.shop import Visitor


class PretixWaitingListServedOracle(Oracle):
    evaluation_timeout_seconds = 900
    readiness_grace_seconds = 300

    FAILURE_CLASSES = {
        "required_application_topology_changed": FailureClass.AGENT_ERROR,
        "deployment_not_ready": FailureClass.AGENT_ERROR,
        "persistent_volumes_replaced_or_missing": FailureClass.AGENT_ERROR,
        "event_not_live": FailureClass.AGENT_ERROR,
        "waiting_list_disabled": FailureClass.AGENT_ERROR,
        "capacity_changed": FailureClass.AGENT_ERROR,
        "waiting_customers_removed": FailureClass.AGENT_ERROR,
        "workshop_closed_with_free_seats": FailureClass.AGENT_ERROR,
        "waiting_list_not_served": FailureClass.AGENT_ERROR,
        "seats_overcommitted": FailureClass.AGENT_ERROR,
        "voucher_not_redeemable": FailureClass.AGENT_ERROR,
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
        claims = json.loads(self.app.command("get", "pvc", "-o", "json"))["items"]
        sizes = self.state()["quotas"]
        self.baseline = {
            "volumes": {p["metadata"]["name"]: p["metadata"]["uid"] for p in claims},
            "sizes": {q: d["size"] for q, d in sizes.items()},
        }

    def state(self):
        return self.app.pretix_python(STATE_SOURCE, {"event": self.problem.EVENT})

    def starved(self, state):
        """Quotas that are closed while free seats are offered to nobody and people are waiting."""
        out = []
        for qid, q in state["quotas"].items():
            if q["closed"] and q["free_unoffered"] > 0 and q["unserved"] > 0:
                out.append(qid)
        return out

    def _platform(self):
        problems = []
        for name, replicas in self.app.topology.items():
            found = self.app.command("get", "deployment", name, "--ignore-not-found", "-o", "json").strip()
            if not found:
                problems.append(("required_application_topology_changed", {"deployment": name, "missing": True}))
                continue
            doc = json.loads(found)
            if doc["spec"].get("replicas", 1) != replicas:
                problems.append(
                    (
                        "required_application_topology_changed",
                        {"deployment": name, "replicas": doc["spec"].get("replicas")},
                    )
                )
            elif doc.get("status", {}).get("readyReplicas", 0) != replicas:
                problems.append(("deployment_not_ready", {"deployment": name}))
        claims = json.loads(self.app.command("get", "pvc", "-o", "json"))["items"]
        now = {p["metadata"]["name"]: p["metadata"]["uid"] for p in claims}
        for name, uid in self.baseline["volumes"].items():
            if now.get(name) != uid:
                problems.append(("persistent_volumes_replaced_or_missing", {"volume": name}))
        return problems

    def _probe(self):
        """Ordinary shop traffic, then the next cron run, then traffic again."""
        base = self.app.front_door()
        item = self.problem.target["item"] if self.problem.target else None
        for _ in range(3):
            v = Visitor(base, self.problem.ORGANIZER, self.problem.EVENT)
            v.visit()
            if item:
                v.get(f"/waitinglist?item={item}")
            time.sleep(2)
        # Wait until the next per-minute cron run has assigned vouchers (it starts on the minute).
        now = time.time()
        time.sleep(60 - now % 60 + 25)
        for _ in range(2):
            Visitor(base, self.problem.ORGANIZER, self.problem.EVENT).visit()
            time.sleep(2)

    def _redeem(self, state):
        """A waiting customer follows their voucher link and registers."""
        t = state["quotas"].get(str(self.problem.target["quota"]))
        offers = [o for o in (t or {}).get("offers", []) if o["code"] and not o["redeemed"]]
        if not offers:
            return None
        offer = offers[0]
        base = self.app.front_door()
        v = Visitor(base, self.problem.ORGANIZER, self.problem.EVENT, email=offer["email"])
        v.visit()
        v.redeem_page(offer["code"])
        ok, msg = v.add_to_cart(self.problem.target["item"], voucher=offer["code"])
        if not ok:
            return {"ok": False, "message": msg, "email": offer["email"]}
        v.checkout()
        code, msg, _ = v.confirm()
        return {"ok": bool(code), "order": code, "message": msg, "email": offer["email"]}

    def evaluate(self) -> dict:
        if not self.baseline:
            return self.fail("baseline_not_captured")
        failures, report = [], {}
        started = time.time()
        try:
            failures += self._platform()
        except Exception as exc:
            failures.append(("probe_failed", {"stage": "platform", "error": repr(exc)}))
        try:
            if self.problem.target:
                self._probe()
            state = self.state()
            report["event"] = state["event"]
            if not state["event"]["live"]:
                failures.append(("event_not_live", {}))
            if not (state["event"]["waiting_list_enabled"] and state["event"]["waiting_list_auto"]):
                failures.append(("waiting_list_disabled", state["event"]))
            changed = {
                q: (self.baseline["sizes"].get(q), d["size"])
                for q, d in state["quotas"].items()
                if self.baseline["sizes"].get(q) != d["size"]
            }
            if changed:
                failures.append(("capacity_changed", {"quotas": changed}))
            # The damage to repair is the incident as handed over: the target, and every workshop the same defect
            # had closed before the agent started (even if reopened since). A workshop that ordinary shop traffic
            # closes the same way for the first time later is reported as a recurrence, not held against the agent.
            handover = getattr(self.problem, "handover_time", None) or started
            target_q = str(self.problem.target["quota"]) if self.problem.target else None
            starved, late = [], []
            for q in self.starved(state):
                closed_at = state["quotas"][q]["first_closed_at"]
                (late if q != target_q and closed_at and closed_at >= handover else starved).append(q)
            report["closed_after_handover"] = late
            report["workshops"] = {
                q: {
                    k: d[k]
                    for k in (
                        "name",
                        "size",
                        "closed",
                        "close_when_sold_out",
                        "sold",
                        "offered",
                        "free_unoffered",
                        "unserved",
                    )
                }
                for q, d in state["quotas"].items()
            }
            if starved:
                failures.append(
                    ("workshop_closed_with_free_seats", {"quotas": {q: report["workshops"][q] for q in starved}})
                )
            target = self.problem.target
            if target:
                t = state["quotas"][str(target["quota"])]
                if t["free_unoffered"] > 0 and t["unserved"] > 0:
                    failures.append(
                        ("waiting_list_not_served", {"free": t["free_unoffered"], "waiting": t["unserved"]})
                    )
                if t["sold"] + t["offered"] > t["size"]:
                    failures.append(
                        ("seats_overcommitted", {"sold": t["sold"], "offered": t["offered"], "size": t["size"]})
                    )
                present = {str(e["id"]) for e in t["entries"]}
                registered = set(t["registered_emails"])
                removed = [
                    i
                    for i in self.problem.handover.get("entries", [])
                    if str(i) not in present and self.problem.handover["emails"].get(i) not in registered
                ]
                if removed:
                    failures.append(("waiting_customers_removed", {"entries": removed}))
                served = [e for e in t["entries"] if e["voucher"]]
                report["target"] = {
                    "close_when_sold_out": t["close_when_sold_out"],
                    "closed": t["closed"],
                    "sold": t["sold"],
                    "offered": t["offered"],
                    "size": t["size"],
                    "served_in_order": _in_order(t["entries"]),
                    "served": len(served),
                    "waiting": t["unserved"],
                }
                if not failures:
                    redeemed = self._redeem(state)
                    report["redemption"] = redeemed
                    if redeemed is not None and not redeemed["ok"]:
                        failures.append(("voucher_not_redeemable", redeemed))
        except Exception as exc:
            failures.append(("probe_failed", {"stage": "state", "error": repr(exc)}))
        target = report.get("target") or {}
        collateral = {
            "close_when_sold_out_left_off": target.get("close_when_sold_out") is False,
            "served_in_order": target.get("served_in_order"),
            "closed_after_handover": report.get("closed_after_handover"),
        }
        if failures:
            reason, detail = failures[0]
            verdict = self.fail(reason, **detail)
            verdict["failures"] = [r for r, _ in failures]
            verdict["collateral"] = collateral
            verdict["report"] = report
            return verdict
        return {"success": True, "collateral": collateral, **report}


def _in_order(entries):
    """True if no entry got a voucher while an earlier one (same priority) still waits."""
    waiting_before = False
    for e in sorted(entries, key=lambda e: (-e["priority"], e["created"])):
        if not e["voucher"]:
            waiting_before = True
        elif waiting_before:
            return False
    return True


STATE_SOURCE = r"""
from django.db.models import Q
from django.utils.timezone import now
from django_scopes import scopes_disabled
from pretix.base.models import CartPosition, Event, LogEntry, Order, OrderPosition, Voucher
with scopes_disabled():
    ev = Event.objects.get(slug=ARGS["event"])
    out = {"event": {"live": ev.live, "waiting_list_enabled": bool(ev.settings.waiting_list_enabled),
                     "waiting_list_auto": bool(ev.settings.waiting_list_auto)}, "quotas": {}}
    t = now()
    for q in ev.quotas.all():
        items = list(q.items.values_list("pk", flat=True))
        sold_qs = OrderPosition.objects.filter(order__event=ev, item_id__in=items,
                                               order__status__in=[Order.STATUS_PAID, Order.STATUS_PENDING])
        sold = sold_qs.count()
        registered = set(sold_qs.values_list("order__email", flat=True))
        vouchers = Voucher.objects.filter(event=ev, block_quota=True).filter(
            Q(item_id__in=items) | Q(quota=q)).filter(Q(valid_until__isnull=True) | Q(valid_until__gte=t))
        offered = sum(max(0, v.max_usages - v.redeemed) for v in vouchers)
        carts = CartPosition.objects.filter(event=ev, item_id__in=items, expires__gte=t).count()
        entries = []
        for e in ev.waitinglistentries.filter(item_id__in=items).select_related("voucher"):
            v = e.voucher
            entries.append({"id": e.pk, "email": e.email, "priority": e.priority, "created": e.created.isoformat(),
                            "voucher": v.pk if v else None, "code": v.code if v else None,
                            "redeemed": bool(v and v.redeemed >= v.max_usages),
                            "valid": bool(v and (v.valid_until is None or v.valid_until >= t))})
        unserved = sum(1 for e in entries if not e["voucher"] and e["email"] not in registered)
        offers = [{"email": e["email"], "code": e["code"], "redeemed": e["redeemed"]} for e in entries
                  if e["voucher"] and e["valid"]]
        closed_log = LogEntry.objects.filter(content_type__model="quota", object_id=q.pk,
                                             action_type="pretix.event.quota.closed").order_by("datetime").first()
        out["quotas"][str(q.pk)] = {
            "first_closed_at": closed_log.datetime.timestamp() if (q.closed and closed_log) else None,
            "name": q.name, "size": q.size, "closed": q.closed, "close_when_sold_out": q.close_when_sold_out,
            "sold": sold, "offered": offered, "carts": carts,
            "free_unoffered": (q.size or 0) - sold - offered - carts if q.size is not None else 0,
            "unserved": unserved, "entries": entries, "offers": offers, "registered_emails": sorted(registered),
        }
    print(json.dumps(out, default=str))
"""
