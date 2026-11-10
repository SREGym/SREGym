"""A per-minute waiting-list cron hands out a seat that a checkout sold at the same instant.

Upstream pretix defect (2026.9.0, found and reproduced by Specula as MC-1, unreported):
``QuotaAvailability._compute`` counts a quota's paid/pending orders, then its blocking
vouchers, then its valid carts in *separate* statements, each taking its own READ
COMMITTED snapshot (``src/pretix/base/services/quotas.py``). The per-minute
``pretix cron`` runs ``assign_automatically`` for the waiting list under the quota's
advisory lock and uses this compute. A checkout whose cart lease still has at least
``LOCK_TRUST_WINDOW`` (120 s) left takes the lock-free fast path -- no advisory lock,
no quota re-check (``orders.py`` ``_check_positions`` lines 779/864) -- and turns the
cart row into an order row in one COMMIT. When that COMMIT lands after the cron's orders
count and before its carts count, the cron sees the unit in neither table, believes one
seat is free and issues a quota-blocking waiting-list voucher for a seat that is already
sold. The voucher redeems without a quota check (blocking vouchers bypass it), so a
second order is created for a quota of one: two paid tickets for one seat. Nothing logs
at WARNING or above, the cron pod is gone within a minute, and the double sale only
surfaces later at the quota page and the door.

Injection fidelity: the trigger is the real code path. ``inject_fault`` fills a workshop
to one seat left, a buyer from outside the cluster holds that seat in a cart, a visitor
joins the waiting list, and the real ``assign_automatically`` (what ``pretix cron`` runs)
is executed while the buyer's fast-path checkout commits between its orders and carts
counts. To make the sub-millisecond interleaving deterministic without altering the
defect, the driver -- a transient conductor-run process in the worker pod -- widens the
gap between the orders count and the carts count with a ``time.sleep`` inside
``QuotaAvailability._compute_carts`` for its own call only; the deployed image and the
real per-minute CronJob are untouched, and the resulting state (a phantom voucher, a
double sale, nothing logged) is identical to the natural race.
"""

import json
import logging
import subprocess
import time

from sregym.conductor.oracles.apps.pretix.quota_integrity import PretixQuotaIntegrityOracle
from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.problems.base import Problem
from sregym.service.apps.pretix.application import PretixApplication
from sregym.service.apps.pretix.shop import ShopTraffic, Visitor
from sregym.utils.decorators import mark_fault_injected

logger = logging.getLogger("all.sregym.problems.pretix")

#: Index of the workshop that sells out and is oversold.
TARGET = 0
#: Seconds the driver holds the race window open while the buyer's checkout commits.
WINDOW_S = 6.0
MAX_ATTEMPTS = 8
#: Let the double sale age before handover: ordinary buyers keep shopping, the cron keeps
#: running every minute and the door keeps scanning, so the oversell is not the newest event.
PROPAGATION_S = 1500


class PretixWaitingListCronPhantomCapacity(Problem):
    ORGANIZER = "lakeside"
    EVENT = "techday26"

    def __init__(self):
        app = PretixApplication()
        super().__init__(app)
        self.kubectl = app.kubectl
        self.faulty_service = "pretix-worker"
        self.propagation_duration_s = PROPAGATION_S
        self.target_quota = None
        self.target_item = None
        self.target_size = None
        self.target_room = None
        self.first_order = None
        self.phantom_order = None
        self.phantom_voucher = None
        self.traffic = None
        self.observed = {}
        self.root_cause = self.build_structured_root_cause(
            component="deployment/pretix-worker",
            namespace=self.namespace,
            description=(
                "A per-minute pretix cron run oversold a workshop by handing a waiting-list voucher to a seat that a "
                "buyer's checkout sold at the same instant. Upstream pretix 2026.9.0 defect: "
                "QuotaAvailability._compute (src/pretix/base/services/quotas.py) counts a quota's paid/pending orders, "
                "then its blocking vouchers, then its valid cart positions in SEPARATE statements, each taking its own "
                "READ COMMITTED snapshot. The per-minute 'pretix cron' runs assign_automatically for the waiting list "
                "(src/pretix/base/services/waitinglist.py) holding the quota's advisory lock and using this compute. A "
                "checkout whose cart lease still had at least LOCK_TRUST_WINDOW (120 s) left took the lock-free FAST "
                "PATH in _perform_order/_check_positions (src/pretix/base/services/orders.py lines 779 and 864): it "
                "took NO advisory lock and did NO quota re-check, and converted the cart row into an order row in a "
                "single COMMIT. That COMMIT landed after the cron's orders count and before its carts count, so the "
                "cron counted the sold unit in neither table, computed one seat as still free, and issued a "
                "quota-blocking waiting-list voucher for a seat that was already sold. The blocking voucher then "
                "redeemed without a quota check, creating a second paid order: two valid tickets on a quota of one. "
                "This is NOT a fix-path bug: the symptom is distant in time and place. The lock-free fast path and the "
                "non-atomic (separate-statement, separate-snapshot) availability count together are the cause. "
                "Distinguish from other oversell explanations that are WRONG here: the two checkouts were not both "
                "locked and serialized; no cart-lease extension or clock stall resurrected an expired cart; no order "
                "change or REST API order edit skipped a quota check; the waiting-list voucher was not issued twice by "
                "an admin and the cron together; the quota was not simply misconfigured too large; and no double-submit "
                "or retry created the duplicate directly. Fix: make the oversold quota consistent again (cancel and, if "
                "money moved, refund the extra waiting-list order; or invalidate the phantom voucher before it is "
                "redeemed), keeping the original buyer's ticket, without taking the shop or the waiting list offline."
            ),
        )
        self.diagnosis_oracle = LLMAsAJudgeOracle(self, self.root_cause)
        self.mitigation_oracle = PretixQuotaIntegrityOracle(self)

    # ------------------------------------------------------------------ helpers
    def _catalog(self):
        if not getattr(self.app, "catalog", None):
            raise RuntimeError("pretix catalog not populated; app was not deployed by this problem")
        return self.app.catalog

    def _fill_to_last_seat(self, item, n):
        return self.app.pretix_python(
            FILL_SOURCE, {"organizer": self.ORGANIZER, "event": self.EVENT, "item": item, "n": n}
        )

    def _front_door(self):
        return self.app.front_door()

    def _api(self):
        return f"{self._front_door()}/api/v1/organizers/{self.ORGANIZER}/events/{self.EVENT}"

    def _token(self):
        import base64

        return base64.b64decode(
            self.app.command("get", "secret", "pretix-api", "-o", "jsonpath={.data.token}")
        ).decode()

    def _bust_cache(self):
        keys = self.app.command(
            "exec", "deploy/redis", "--", "redis-cli", "-n", "1", "keys", "item_quota_cache:*"
        ).split()
        if keys:
            self.app.command("exec", "deploy/redis", "--", "redis-cli", "-n", "1", "del", *keys)
        self.app.command("exec", "deploy/redis", "--", "redis-cli", "-n", "1", "del", "quotas:1:availabilitycache")

    def _driver_program(self, entry):
        params = {"organizer": self.ORGANIZER, "event": self.EVENT, "entry": entry, "window": WINDOW_S}
        return (
            "import os,sys;os.chdir('/pretix/src');sys.path.insert(0,'/pretix/src');"
            "os.environ.setdefault('DJANGO_SETTINGS_MODULE','production_settings');os.environ.setdefault('DATA_DIR','/data/');"
            "import django;django.setup()\n" + f"PARAMS={params!r}\n" + DRIVER_SOURCE
        )

    def _race_once(self, base, item, quota, entry, a):
        """Run the real waiting-list cron compute with the window held open; the buyer commits inside it."""
        proc = subprocess.Popen(
            [
                "kubectl",
                "-n",
                self.namespace,
                "exec",
                "-i",
                "deploy/pretix-worker",
                "-c",
                "worker",
                "--",
                "python",
                "-",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        proc.stdin.write(self._driver_program(entry))
        proc.stdin.close()
        voucher = None
        a_code = None
        while True:
            line = proc.stdout.readline()
            if not line:
                break
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if d.get("window_open"):
                a_code, _, _ = a.confirm()  # the buyer's fast-path checkout commits during the window
            elif "voucher" in d:
                voucher = d["voucher"]
        proc.wait()
        return a_code, voucher

    # ------------------------------------------------------------------ lifecycle
    @mark_fault_injected
    def inject_fault(self):
        catalog = self._catalog()
        base = self._front_door()
        workshop = catalog["workshops"][TARGET]
        self.target_item = workshop["item"]
        self.target_quota = workshop["quota"]
        self.target_size = workshop["seats"]
        self.target_room = workshop["room"]

        # Ordinary buyer traffic on the other workshops, from outside the cluster, throughout.
        self.traffic = ShopTraffic(self.app, base, workers=2, interval=(5.0, 14.0), exclude_items={self.target_item})
        self.traffic.start()

        # The workshop sells out: all but one seat already sold.
        fill = self._fill_to_last_seat(self.target_item, self.target_size - 1)
        self.observed["filled"] = fill.get("made")

        for attempt in range(1, MAX_ATTEMPTS + 1):
            # A buyer holds the last seat in a cart (valid 30-min lease => lock-free fast path).
            a = Visitor(base, self.ORGANIZER, self.EVENT)
            a.visit()
            if not a.add_to_cart(self.target_item)[0]:
                # Someone else took the last seat; free it and retry.
                self._cancel_open_orders_for_item(self.target_item, keep=self.target_size - 1)
                continue
            a.checkout()
            # A visitor finds it sold out and joins the waiting list.
            entry = None
            for _ in range(5):
                self._bust_cache()
                w = Visitor(base, self.ORGANIZER, self.EVENT)
                w.visit()
                if w.join_waitinglist(self.target_item):
                    entry = self._latest_waitlist_entry(self.target_item)
                    break
                time.sleep(2)
            if not entry:
                raise RuntimeError("could not form a waiting list on the target workshop")

            a_code, voucher = self._race_once(base, self.target_item, self.target_quota, entry, a)
            if not voucher:
                self.observed[f"attempt_{attempt}"] = "cron did not hand out a phantom voucher"
                # A's order now holds the seat; free it and retry.
                self._cancel_order(a_code)
                continue

            self.first_order = a_code
            self.phantom_voucher = voucher
            # The waiting-list customer follows the email link and buys: the second sale of the one seat.
            wbuyer = Visitor(base, self.ORGANIZER, self.EVENT, email=w.email)
            wbuyer.visit()
            wbuyer.redeem_page(voucher)
            if not wbuyer.add_to_cart(self.target_item, voucher=voucher)[0]:
                raise RuntimeError("phantom voucher did not redeem")
            wbuyer.checkout()
            self.phantom_order, _, _ = wbuyer.confirm()
            avail = self._availability(self.target_quota)
            self.observed["attempts"] = attempt
            self.observed["availability"] = avail
            self.observed["first_order"] = self.first_order
            self.observed["phantom_order"] = self.phantom_order
            logger.info(
                "pretix oversell established: quota %s size %s availability %s (orders A=%s, W=%s)",
                self.target_quota,
                self.target_size,
                avail,
                self.first_order,
                self.phantom_order,
            )
            if (avail["paid_orders"] + avail["pending_orders"]) <= self.target_size:
                raise RuntimeError(f"expected an oversold quota, got {avail}")
            return
        raise RuntimeError(f"phantom capacity not triggered in {MAX_ATTEMPTS} attempts: {json.dumps(self.observed)}")

    @mark_fault_injected(strict=True)
    def recover_fault(self):
        """Reference mitigation: cancel the phantom waiting-list order, keep the original buyer, leave the shop up."""
        if self.traffic:
            self.traffic.stop()
        if self.mitigation_oracle.evaluate().get("success"):
            return
        if self.phantom_order:
            self._cancel_order(self.phantom_order)
        if self.phantom_voucher:
            self._disable_voucher(self.phantom_voucher)

    # ------------------------------------------------------------------ API helpers
    def _api_call(self, method, path, body=None):
        import urllib.request

        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            self._api() + path,
            data=data,
            method=method,
            headers={"Authorization": f"Token {self._token()}", "Host": "pretix-web"},
        )
        if data:
            req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=90) as r:
            return json.loads(r.read() or b"null")

    def _availability(self, quota):
        return self._api_call("GET", f"/quotas/{quota}/availability/")

    def _latest_waitlist_entry(self, item):
        res = self._api_call("GET", f"/waitinglistentries/?item={item}&ordering=-id")["results"]
        return res[0]["id"] if res else None

    def _cancel_order(self, code):
        if not code:
            return
        try:
            self._api_call("POST", f"/orders/{code}/mark_canceled/", {"send_email": False})
        except Exception as exc:
            logger.warning("cancel order %s failed: %s", code, exc)

    def _cancel_open_orders_for_item(self, item, keep):
        orders = self._api_call("GET", f"/orders/?item={item}&status=n")["results"]
        for o in orders:
            self._cancel_order(o["code"])

    def _disable_voucher(self, code):
        try:
            v = self._api_call("GET", f"/vouchers/?code={code}")["results"]
            if v:
                self._api_call(
                    "PATCH",
                    f"/vouchers/{v[0]['id']}/",
                    {"max_usages": v[0]["redeemed"] or 1, "valid_until": "2000-01-01T00:00:00Z"},
                )
        except Exception as exc:
            logger.warning("disable voucher %s failed: %s", code, exc)


FILL_SOURCE = r"""
from django_scopes import scopes_disabled, scope
from django.utils.timezone import now
from decimal import Decimal
from pretix.base.models import Event, Item, Order, OrderPosition
with scopes_disabled():
    ev = Event.objects.select_related("organizer").get(organizer__slug=ARGS["organizer"], slug=ARGS["event"])
with scope(organizer=ev.organizer):
    item = Item.objects.get(pk=ARGS["item"]); n = ARGS["n"]
    ch = ev.organizer.sales_channels.get(identifier="web")
    existing = OrderPosition.objects.filter(order__event=ev, item=item,
                                            order__status__in=[Order.STATUS_PAID, Order.STATUS_PENDING]).count()
    made = []
    for i in range(max(0, n - existing)):
        o = Order.objects.create(code=Order.normalize_code(f"SOLD{ARGS['item']}X{i:03d}"), event=ev,
            status=Order.STATUS_PAID, email=f"attendee-{ARGS['item']}-{i}@lakeside-techday.test",
            datetime=now(), expires=now(), total=Decimal("0.00"), sales_channel=ch, locale="en")
        OrderPosition.objects.create(order=o, item=item, price=Decimal("0.00"), positionid=1)
        made.append(o.code)
    print(json.dumps({"made": len(made), "avail": list(item.check_quotas(count_waitinglist=False))}))
"""

DRIVER_SOURCE = r"""
import json, time
from django_scopes import scopes_disabled
from pretix.base.models import Event, WaitingListEntry
from pretix.base.services import quotas as Q
from pretix.base.services.waitinglist import assign_automatically
with scopes_disabled():
    event = Event.objects.select_related("organizer").get(organizer__slug=PARAMS["organizer"], slug=PARAMS["event"])
entry = PARAMS["entry"]; win = PARAMS["window"]
orig = Q.QuotaAvailability._compute_carts
state = {"armed": True}
def patched(self, quotas, q_items, q_vars, size_left, now_dt):
    # Hold the real race window open: orders were counted before this, carts are counted after.
    if state["armed"]:
        state["armed"] = False
        print(json.dumps({"window_open": True}), flush=True)
        time.sleep(win)
    return orig(self, quotas, q_items, q_vars, size_left, now_dt)
Q.QuotaAvailability._compute_carts = patched
assign_automatically(event.pk)
with scopes_disabled():
    v = WaitingListEntry.objects.filter(pk=entry, voucher__isnull=False).values_list("voucher__code", flat=True).first()
print(json.dumps({"voucher": v}), flush=True)
"""
