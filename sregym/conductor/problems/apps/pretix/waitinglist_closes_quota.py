"""A workshop with free seats is shown sold out and its waiting list is never served.

Upstream pretix defect (2026.9.0, found by Specula as CR-11 from code review, reproduced on
this stack, unreported): ``QuotaAvailability._compute_waitinglist``
(``src/pretix/base/services/quotas.py``) subtracts the waiting-list entries that have no
voucher yet from the free units and reports ``(AVAILABILITY_ORDERED, 0)`` when they use up
the rest. ``compute()`` calls ``_close()`` after every fresh computation, and ``_close``
permanently closes any ``close_when_sold_out`` quota whose result is ``<= ORDERED``. Carts
alone give ``RESERVED`` (deliberately not a close), but waiting-list entries give
``ORDERED``. So when the last seats sit in carts, visitors join the waiting list
(the shop offers it for "reserved" products), and the carts then expire, the next ordinary
availability computation that counts the waiting list -- a shop page view, a cart-add
attempt, a waiting-list join -- closes the quota although it was never sold out. A
closed quota reports 0 to ``assign_automatically`` (the per-minute cron's waiting-list
assignment, which counts without the waiting list), so the waiting customers never get a
voucher and the free seats are never sold; the shop shows the workshop as sold out. The
only trace is one ``pretix.event.quota.closed`` log entry with no user, the same as a real
sell-out; the cron logs nothing. A plain reopen does not help: the API's quota update
(``QuotaViewSet.perform_update`` -> ``Quota.rebuild_cache``) and the control panel's quota
page recompute availability with the waiting list immediately, so the quota closes again
within the same request (verified on this stack: ``quota.opened`` and ``quota.closed``
14 ms apart), and any later page view does the same until the waiting list holds vouchers.

Scenario and trigger (ordinary traffic only, nothing is forced): the organizer runs every
workshop quota with "close this quota permanently once it is sold out" and a 10-minute
cart reservation. Registrations from the presale period are seeded as ordinary shop
registrations; the target workshop has two seats left. Two visitors put the last two
seats into their carts and abandon them; over the next minutes several visitors find the
workshop "reserved" and join its waiting list. When the carts lapse, the next visitor's
shop page view closes the quota. The problem only chooses when the abandoned carts are
created (20 s past a minute), so that the carts lapse well before the next per-minute
cron run and a page view, not the cron, computes first -- in a busy shop the page view
wins this race almost always anyway. Buyers keep registering for the other workshops, the
door keeps admitting attendees, a visitor occasionally joins the waiting list, and the
incident ages (``propagation_duration_s``) before handover. The door station's room
display warns once people have waited 20 minutes for a room with free seats that nobody
has been offered. The background buyers hit the same defect on their own now and then
(in 3 of 5 validation runs one or two further workshops closed this way before handover);
those are part of the incident and the oracle requires them repaired too, while a workshop
that closes this way only after handover is reported as a recurrence, not failed.

Repair (the oracle accepts all of these; each was tested on this stack): reopen the quota
and get the waiting list served before the next availability computation re-closes it --
for example reopen it with "close when sold out" switched off in the same change (control
panel "reopen and disable", or ``PATCH /quotas/{id}/ {"closed": false,
"close_when_sold_out": false}``) and let the cron (or "assign vouchers automatically")
send the vouchers, optionally switching the setting back on afterwards; or, in a Django
shell, reopen and run ``assign_automatically`` in one go. Once the vouchers hold the free
seats the quota may close again legitimately; blocking waiting-list vouchers still redeem.

Relation to the existing pretix problem (``pretix_waitinglist_cron_phantom_capacity``):
different defect (closing logic, not the split availability count), opposite symptom
(seats unsold and the waiting list starved, rather than a seat sold twice), different
repair (reopen and serve the waiting list, nothing is cancelled).
"""

import base64
import json
import logging
import random
import threading
import time
import urllib.request

from sregym.conductor.oracles.apps.pretix.waitinglist_served import PretixWaitingListServedOracle
from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.problems.base import Problem
from sregym.service.apps.pretix.application import PretixApplication
from sregym.service.apps.pretix.shop import ShopTraffic, Visitor
from sregym.utils.decorators import mark_fault_injected

logger = logging.getLogger("all.sregym.problems.pretix")

DESCRIPTION = (
    "pretix 2026.9.0, an open-source ticket shop, selling registrations for the Lakeside Tech Day 2026 workshops "
    "(organizer lakeside, event techday26; each workshop is a free product with a limited number of seats). It is "
    "deployed like pretix's production docker setup: pretix-web (nginx in front of gunicorn, 12 workers x 16 threads) "
    "behind the pretix-web Service, pretix-worker (Celery task worker, concurrency 4), the pretix-periodic CronJob "
    "that runs pretix cron every minute, PostgreSQL (postgres), Redis (redis, with append-only persistence) and the "
    "Mailpit SMTP sink (mailpit) that receives all outgoing email. Attendees register through the public shop and "
    "join waiting lists; the door-scanner deployment runs the door scanning stations, which check attendees in "
    "through the check-in API and show the workshop rooms' check-in lists and waiting lists. The organizer's API "
    "token is in the pretix-api Secret (key token); send it as 'Authorization: Token <token>'."
)

#: Seats left in the target workshop when the abandoned carts take them.
LEFT = 2
#: Waiting-list entries formed while the carts hold the seats.
WAITING = 4
RESERVATION_MIN = 10
OTHER_FILL = (0.5, 0.85)
PROPAGATION_S = 1500

ROOT_CAUSE = (
    "A workshop's quota was closed although seats were free, so the shop shows it sold out and its waiting list is "
    "never served. Upstream pretix 2026.9.0 defect in the quota availability computation "
    "(src/pretix/base/services/quotas.py): _compute_waitinglist subtracts the waiting-list entries without a voucher "
    "from the free units and reports AVAILABILITY_ORDERED (0 left) when they use up the remaining seats, and "
    "compute() calls _close() after every fresh computation, which permanently closes a close_when_sold_out quota "
    "whose result is <= ORDERED. Here the last two seats were held in carts (RESERVED, which does not close the "
    "quota), visitors joined the waiting list, the carts expired, and the next ordinary availability computation "
    "that counts the waiting list (a shop page view / cart-add attempt / waiting-list join) saw the waiting-list "
    "entries alone consume the free seats and closed the quota (one 'pretix.event.quota.closed' log entry with no "
    "user), with fewer paid tickets than seats. A closed quota reports 0 to the waiting-list assignment that the "
    "per-minute pretix cron runs (assign_automatically, which counts without the waiting list), so no vouchers are "
    "sent and the free seats stay unsold, permanently. A plain reopen is undone at once: the next availability "
    "computation that counts the waiting list (the reopen request itself via Quota.rebuild_cache, the quota page, "
    "any shop view) closes it again while the entries are unserved. Fix: reopen the quota and serve the waiting list "
    "before anything recomputes it (e.g. reopen with close_when_sold_out switched off and let the cron or 'assign "
    "automatically' send the vouchers, optionally re-enabling the setting afterwards; or reopen and run "
    "assign_automatically in one shell step), without deleting the waiting customers, changing the room capacity, "
    "or turning the waiting list off. "
    "Ordinary shop traffic can close further workshops the same way; a diagnosis may name one or several of them. "
    "Grading requirements: localization (D1) is correct if it names pretix's quota availability / close-when-sold-out "
    "logic (pretix-web, where the shop computation closed it, or pretix-worker/pretix-periodic, whose waiting-list "
    "assignment then sees the closed quota). The mechanism core is REQUIRED for fault characterization (D2-Q1 and "
    "D2-Q3): the quota was closed by close_when_sold_out although tickets were never sold out, because the "
    "availability computation counts the pending waiting-list entries against the free seats (after the carts "
    "expired the waiting list alone made it look sold out), and once closed the cron's waiting-list assignment sees "
    "no free seats, so the list is never served. Equivalent wording is accepted (for example 'the waiting-list "
    "entries are counted as consuming the seats, which triggered close-when-sold-out'). The deeper detail (the "
    "waiting-list count yields AVAILABILITY_ORDERED while carts yield RESERVED, _close closes at <= ORDERED after "
    "every compute, and a plain reopen is re-closed by the next computation) earns D2-Q2 but is not required if the "
    "core is right. These plausible explanations are WRONG and must get No on D2-Q1, D2-Q2 and D2-Q3: (a) the "
    "workshop really sold out and later cancellations or expired orders freed the seats, which close_when_sold_out "
    "then keeps closed by design (paid tickets never reached the size); (b) the cron or Celery worker is not running "
    "or the waiting-list assignment is disabled/misconfigured (it runs every minute and sees a closed quota); (c) a "
    "stale Redis availability cache shows it sold out (the quota row itself is closed); (d) the abandoned carts "
    "still hold the seats (they expired); (e) an operator or API client closed the quota manually (the log entry "
    "has no user or token). A diagnosis that only says 'the quota was closed because of close_when_sold_out' without "
    "the waiting-list entries causing the close gets No on D2-Q1."
)


class PretixWaitingListClosesQuota(Problem):
    ORGANIZER = "lakeside"
    EVENT = "techday26"

    def __init__(self):
        app = PretixApplication(
            {
                "description": DESCRIPTION,
                "close_when_sold_out": True,
                "reservation_minutes": RESERVATION_MIN,
                "scanner_waitlist_watch": True,
            }
        )
        super().__init__(app)
        self.kubectl = app.kubectl
        self.faulty_service = "pretix-web"
        self.propagation_duration_s = PROPAGATION_S
        self.target = None
        self.buyers = None
        self.trickle = None
        self._stop = threading.Event()
        self.handover = {}
        self.handover_time = None
        self.observed = {}
        self.root_cause = self.build_structured_root_cause(
            component="deployment/pretix-web", namespace=self.namespace, description=ROOT_CAUSE
        )
        self.diagnosis_oracle = LLMAsAJudgeOracle(self, self.root_cause)
        self.mitigation_oracle = PretixWaitingListServedOracle(self)

    # ------------------------------------------------------------------ helpers
    def _token(self):
        return base64.b64decode(
            self.app.command("get", "secret", "pretix-api", "-o", "jsonpath={.data.token}")
        ).decode()

    def api(self, method, path, body=None):
        url = f"{self.app.front_door()}/api/v1/organizers/{self.ORGANIZER}/events/{self.EVENT}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={"Authorization": f"Token {self._token()}"})
        if data:
            req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=90) as r:
            raw = r.read()
            return json.loads(raw) if raw else None

    def _entries(self, item):
        return self.api("GET", f"/waitinglistentries/?item={item}&page_size=200")["results"]

    def _closed(self, quota):
        return self.app.psql(f"SELECT closed FROM pretixbase_quota WHERE id={int(quota)}").strip() == "t"

    def _join(self, base, item):
        """A visitor tries the workshop, finds it unavailable and puts their name on the waiting list."""
        v = Visitor(base, self.ORGANIZER, self.EVENT)
        v.visit()
        time.sleep(random.uniform(2, 8))
        ok, _ = v.add_to_cart(item)
        if ok:
            return "bought"  # must not happen while the seats are held or the quota is closed
        return "joined" if v.join_waitinglist(item) else "refused"

    def _trickle(self, base, item):
        while not self._stop.wait(random.uniform(240, 480)):
            try:
                self._join(base, item)
            except Exception as exc:
                logger.debug("waiting-list visitor failed: %s", exc)

    # ------------------------------------------------------------------ lifecycle
    @mark_fault_injected
    def inject_fault(self):
        app = self.app
        base = app.front_door()
        workshops = app.catalog["workshops"]
        candidates = [w for w in workshops if w["seats"] >= 14]
        target = random.choice(candidates)
        fills = {w["item"]: int(w["seats"] * random.uniform(*OTHER_FILL)) for w in workshops}
        fills[target["item"]] = target["seats"] - LEFT
        app.seed_history(fills)
        self.target = {
            "item": target["item"],
            "quota": target["quota"],
            "seats": target["seats"],
            "room": target["room"],
            "title": target["title"],
        }

        self.buyers = ShopTraffic(app, base, workers=3, interval=(4.0, 10.0), exclude_items={target["item"]})
        self.buyers.start()
        time.sleep(random.uniform(90, 180))

        # Two visitors take the last seats into their carts at 20 s past a minute and walk away.
        while int(time.time()) % 60 != 20:
            time.sleep(0.2)
        held = []
        for _ in range(LEFT):
            v = Visitor(base, self.ORGANIZER, self.EVENT)
            v.visit()
            ok, msg = v.add_to_cart(target["item"])
            if not ok:
                raise RuntimeError(f"could not hold the last seats: {msg}")
            held.append(v)
        lapse = time.time() + RESERVATION_MIN * 60
        self.observed["carts_at"] = time.time()

        # Visitors find the workshop reserved and join its waiting list over the next minutes.
        outcomes = []
        while len([o for o in outcomes if o == "joined"]) < WAITING and time.time() < lapse - 90:
            time.sleep(random.uniform(25, 70))
            outcomes.append(self._join(base, target["item"]))
        self.observed["joins"] = outcomes
        if "bought" in outcomes or outcomes.count("joined") < LEFT:
            raise RuntimeError(f"waiting list did not form: {outcomes}")

        # The carts lapse; the next visitor's page view counts the waiting list.
        while time.time() < lapse + 1.5:
            time.sleep(0.2)
        Visitor(base, self.ORGANIZER, self.EVENT).visit()
        time.sleep(2)
        entries = self._entries(target["item"])
        closed = self._closed(target["quota"])
        self.observed["closed"] = closed
        self.observed["vouchers"] = [e["voucher"] for e in entries]
        if not closed or any(e["voucher"] for e in entries):
            raise RuntimeError(f"quota did not close on the waiting list: closed={closed} entries={entries}")
        self.observed["closed_at"] = time.time()

        self._stop.clear()
        self.trickle = threading.Thread(target=self._trickle, args=(base, target["item"]), daemon=True)
        self.trickle.start()
        self.handover = {"entries": [e["id"] for e in entries], "emails": {e["id"]: e["email"] for e in entries}}
        # The agent starts after the propagation window; workshops closed by then are part of the incident.
        self.handover_time = time.time() + self.propagation_duration_s
        logger.info(
            "pretix quota %s closed by its waiting list (%d entries, %d seats free)",
            target["quota"],
            len(entries),
            LEFT,
        )

    @mark_fault_injected(strict=True)
    def recover_fault(self):
        """Reference mitigation: reopen with close-when-sold-out off, let the cron serve the list, restore the setting."""
        self._stop.set()
        if self.buyers:
            self.buyers.stop()
        if self.mitigation_oracle.evaluate().get("success"):
            return
        oracle = self.mitigation_oracle
        quotas = set(oracle.starved(oracle.state())) | {str(self.target["quota"])}
        for quota in quotas:
            self.api("PATCH", f"/quotas/{quota}/", {"closed": False, "close_when_sold_out": False})
        deadline = time.time() + 150
        while time.time() < deadline:
            state = oracle.state()["quotas"]
            if all(state[q]["free_unoffered"] <= 0 or state[q]["unserved"] == 0 for q in quotas):
                break
            time.sleep(5)
        for quota in quotas:
            self.api("PATCH", f"/quotas/{quota}/", {"close_when_sold_out": True})
