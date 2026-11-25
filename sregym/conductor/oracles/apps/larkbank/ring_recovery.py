"""Grade putting Larkbank's customers right by what each of them is owed.

1. The app stayed up: between the handover and the submission there was no
   stretch longer than MAX_GAP_SECONDS in which no customer's app got an
   answer. (An answer that shows nothing is still an answer.)
2. Hold: customers go on for HOLD_SECONDS after the submission, and standing
   orders go on being paid.
3. Then, with customers paused, by what every customer's app shows:
   - everything a customer had set up before the incident is there, and so is
     what they have added since; a standing order they cancelled is not;
   - nothing is there twice. A customer who could not see a payee or a
     standing order set it up again; once the old data can be read, the
     customer has both. (What a customer set up again in the last
     LATE_SECONDS before the submission is not held against the repair.)
4. By what the payment rail took:
   - no standing order was paid from both of its copies once the customer
     had been seeing both for TWICE_GRACE_SECONDS (counted from the handover
     for a pair the customer was already seeing twice then);
   - every standing order a customer has was paid in its last period.
5. The three extra nodes are in the ring, up and normal (if every customer's
   data can be read, they hold their share: a fifth of the ring has no other
   replica), and a node added now would take its share of the data before
   serving: the ConfigMap nodes start from no longer says otherwise. How the
   nodes already there were started is not held against a repair that put the
   data onto them another way.
"""

import subprocess
import time

from sregym.conductor.oracles.failure import FailureClass
from sregym.conductor.oracles.mitigation import MitigationOracle
from sregym.service.apps.larkbank.application import EVERY_SECONDS, NODES_WANTED

MAX_GAP_SECONDS = 10
HOLD_SECONDS = 45
LATE_SECONDS = 30
TWICE_GRACE_SECONDS = 180

REASONS = (
    "service_interrupted",
    "app_not_answering",
    "customer_data_missing",
    "customer_changes_lost",
    "cancelled_came_back",
    "set_up_twice",
    "paid_twice",
    "standing_orders_not_paid",
    "nodes_missing",
    "adding_a_node_would_repeat",
)


def longest_gap(times, start, end, pauses=()):
    """The longest stretch between start and end with none of the times in it.

    pauses: (from, to) stretches in which customers were held still by an earlier grading; time inside
    them is not counted.
    """

    def length(a, b):
        return (b - a) - sum(max(0.0, min(b, q) - max(a, p)) for p, q in pauses)

    marks = [start, *sorted(t for t in times if start <= t <= end), end]
    return max((length(a, b) for a, b in zip(marks, marks[1:], strict=False)), default=0.0)


def shown(view):
    """How many times a customer's app shows each payee and each standing order, by what it says."""
    payees, orders, by_id = {}, {}, {}
    for payee in view.get("payees") or []:
        key = f"{payee['sort_code']}|{payee['account_number']}"
        by_id[payee["id"]] = key
        payees[key] = payees.get(key, 0) + 1
    for order in view.get("standing_orders") or []:
        if order.get("payee_id") in by_id:  # a standing order whose payee is gone pays nobody
            key = f"{by_id[order['payee_id']]}|{order['amount']}|{order['reference']}"
            orders[key] = orders.get(key, 0) + 1
    return payees, orders


def judge(truth, final, submitted, ended, ring, setting, handover_at=0.0):
    """What is wrong, by reason, in REASONS' order. Empty if customers have what they are owed.

    truth: the world's record. final: what every customer's app shows. submitted, ended: the world's clock
    when the repair was handed over and when customers were paused. ring: the ring's nodes as nodetool
    status gives them. setting: application.setting_in_force(). handover_at: when the agent took over; a
    pair a customer was already seeing twice then is given its time from that moment.
    """
    found = {reason: [] for reason in REASONS}
    views = {view["customer"]: view for view in final}

    paid = {}  # (customer, what the standing order says) -> [(when, standing order id)]
    for payment in truth["payments"]:
        key = f"{payment['sort_code']}|{payment['account_number']}|{int(payment['amount'])}|{payment['reference']}"
        paid.setdefault((payment["customer"], key), []).append((payment["at"], payment.get("standing_order_id")))

    for customer in truth["customers"]:
        name = customer["id"]
        view = views.get(name, {})
        if view.get("payees_status") != 200 or view.get("standing_orders_status") != 200:
            found["app_not_answering"].append(
                {"customer": name, "status": [view.get("payees_status"), view.get("standing_orders_status")]}
            )
            continue
        payees, orders = shown(view)
        for item in customer["items"]:
            what = {"customer": name, item["kind"]: item["key"]}
            times = (orders if item["kind"] == "standing_order" else payees).get(item["key"], 0)
            if item.get("cancelled"):
                if times:
                    found["cancelled_came_back"].append(what)
                continue
            if times == 0:
                found["customer_data_missing" if item["origin"] == "before" else "customer_changes_lost"].append(what)
            elif times > 1 and item.get("redone", 0) <= submitted - LATE_SECONDS:
                found["set_up_twice"].append({**what, "times": times})
            if item["kind"] != "standing_order":
                continue
            payments = paid.get((name, item["key"]), [])
            seen_twice = item.get("twice_at") or 0
            if seen_twice:
                since = max(seen_twice, handover_at) + TWICE_GRACE_SECONDS
                copies = {order for at, order in payments if at > since}
                if len(copies) > 1:
                    found["paid_twice"].append({**what, "from_standing_orders": sorted(copies)})
            # Paid in its last period, unless the customer added it moments ago.
            recent = [at for at, _ in payments if at >= ended - EVERY_SECONDS - 45]
            if times and not recent and ended - (item.get("made_at") or 0) > 30:
                found["standing_orders_not_paid"].append(what)

    normal = [node for node in ring if node["state"] == "UN"]
    if len(normal) != NODES_WANTED or len(ring) != NODES_WANTED:
        found["nodes_missing"].append(
            {"wanted": NODES_WANTED, "up_and_normal": len(normal), "states": sorted(node["state"] for node in ring)}
        )
    wrong = sorted(name for name, value in setting.items() if value is not True)
    if wrong:
        found["adding_a_node_would_repeat"].append({"auto_bootstrap_not_on_in": wrong})
    return {reason: {"count": len(found[reason]), "sample": found[reason][:10]} for reason in REASONS if found[reason]}


class RingRecoveryOracle(MitigationOracle):
    importance = 1.0
    evaluation_timeout_seconds = 900

    FAILURE_CLASSES = dict.fromkeys(REASONS, FailureClass.AGENT_ERROR)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        #: Stretches of the world's clock in which a grading held customers still.
        self.pauses = []

    def evaluate(self) -> dict:
        print("== Larkbank ring recovery ==")
        try:
            return self._evaluate(self.problem.app)
        except Exception as exc:  # noqa: BLE001
            print(f"[FAIL] grading error: {exc}")
            return self.fail_from_exception(exc)

    def _evaluate(self, app) -> dict:
        problem = self.problem
        submitted = app.world_clock()
        reads = [read[1] for read in app.truth()["reads"] if read[2] == 200 and read[3] == 200]
        worst = longest_gap(reads, problem.handover_at, submitted, self.pauses)
        print(f"longest time no customer's app got an answer: {worst:.1f}s (limit {MAX_GAP_SECONDS}s)")
        if worst > MAX_GAP_SECONDS:
            print("❌ the app was not answering")
            return self.fail("service_interrupted", longest_gap_seconds=round(worst, 1))

        result = super().evaluate()
        if not result.get("success"):
            return result

        print(f"hold: customers go on for {HOLD_SECONDS}s")
        time.sleep(HOLD_SECONDS)
        paused = app.world_clock()
        app.world_post("/phase?name=stop")
        try:
            time.sleep(8)  # the scheduler's round in hand
            ended = app.world_clock()
            found = judge(
                app.truth(), app.final(), submitted, ended, self.ring(app), self.setting(app), problem.handover_at
            )
        finally:
            # Customers go back to the app, so the repair can be graded again (after the reference repair,
            # when the problem is validated); the stretch they were held still is not held against it.
            app.world_post("/phase?name=live")
            self.pauses.append((paused, app.world_clock()))
        if found:
            first = next(iter(found))
            print(f"❌ customers do not have what they are owed: {found}")
            return self.fail(first, **found)
        print("✅ the app stayed up, customers have what they are owed once, and the ring has its six nodes")
        return {"success": True, "longest_gap_seconds": round(worst, 1)}

    @staticmethod
    def ring(app):
        try:
            return app.ring_status()
        except (subprocess.SubprocessError, ValueError) as error:
            print(f"the ring did not answer: {error}")
            return []

    @staticmethod
    def setting(app):
        try:
            return app.setting_in_force()
        except (subprocess.SubprocessError, ValueError) as error:
            print(f"the ring's settings could not be read: {error}")
            return {"planned": None}
