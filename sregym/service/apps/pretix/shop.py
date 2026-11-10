"""Shop visitors for the pretix workshop event, driven from the conductor host.

Runs outside the cluster and reaches pretix through the NodePort front door, the
way customers on the internet do: browser sessions with cookies and CSRF tokens,
the shop's AJAX endpoints for add-to-cart and order placement, the public
waiting-list form, and voucher links from the waiting-list emails (read from the
Mailpit sink). Nothing here is installed in the cluster.
"""

from __future__ import annotations

import logging
import random
import re
import threading
import time
import uuid

import requests

logger = logging.getLogger("all.sregym.apps.pretix.shop")

USER_AGENTS = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/19.0 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64; rv:144.0) Gecko/20100101 Firefox/144.0",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 19_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/19.0 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Linux; Android 16; Pixel 9) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Mobile Safari/537.36",
)
FIRST = (
    "alex",
    "sam",
    "maria",
    "jonas",
    "lea",
    "noah",
    "emma",
    "li",
    "omar",
    "sofia",
    "lukas",
    "mia",
    "ben",
    "anna",
    "paul",
    "nina",
    "tom",
    "lena",
    "max",
    "ida",
    "felix",
    "clara",
    "david",
    "eva",
    "jan",
    "zoe",
    "ali",
    "sara",
)
LAST = (
    "schmidt",
    "meyer",
    "khan",
    "novak",
    "rossi",
    "garcia",
    "wang",
    "larsen",
    "silva",
    "becker",
    "kowalski",
    "dubois",
    "nguyen",
    "fischer",
    "jensen",
    "costa",
    "weber",
    "ito",
    "moreau",
    "berg",
)
DOMAINS = ("example.org", "mail.test", "corp.test", "uni.test", "posteo.test")
ORDER_URL_RE = re.compile(r"/order/([A-Z0-9]+)/([a-z0-9]+)/")
VOUCHER_RE = re.compile(r"voucher=([A-Z0-9]+)")


def random_email():
    tag = uuid.uuid4().hex[:4]
    return f"{random.choice(FIRST)}.{random.choice(LAST)}.{tag}@{random.choice(DOMAINS)}"


class ShopError(RuntimeError):
    pass


class Visitor:
    """One browser session in the public shop."""

    def __init__(self, base, organizer, event, email=None):
        self.host = base
        self.shop = f"{base}/{organizer}/{event}"
        self.email = email or random_email()
        self.s = requests.Session()
        self.s.headers["User-Agent"] = random.choice(USER_AGENTS)
        self.s.headers["Accept-Language"] = "en-US,en;q=0.9"
        self.cart_created = None

    def _csrf(self):
        return self.s.cookies.get("pretix_csrftoken", "")

    def get(self, path, **kw):
        r = self.s.get(self.shop + path, timeout=60, **kw)
        if r.status_code >= 500:
            raise ShopError(f"GET {path}: HTTP {r.status_code}")
        return r

    def post(self, path, data, **kw):
        data = {**data, "csrfmiddlewaretoken": self._csrf()}
        r = self.s.post(self.shop + path, data=data, timeout=60, headers={"Referer": self.shop + "/"}, **kw)
        if r.status_code >= 500:
            raise ShopError(f"POST {path}: HTTP {r.status_code}")
        return r

    def _wait_async(self, first, timeout=60):
        data = first
        deadline = time.monotonic() + timeout
        while not data.get("ready"):
            if time.monotonic() > deadline:
                raise ShopError("async task did not finish")
            time.sleep(0.5)
            r = self.s.get(self.host + data["check_url"], timeout=60)
            data = r.json()
            data.setdefault("check_url", first.get("check_url"))
        return data

    def visit(self):
        return self.get("/").text

    def add_to_cart(self, item, voucher=None):
        fields = {f"item_{item}": "1", "ajax": "1"}
        if voucher:
            fields["_voucher_code"] = voucher
        r = self.post("/cart/add", fields)
        result = self._wait_async(r.json())
        if result.get("success"):
            self.cart_created = time.time()
        return bool(result.get("success")), result.get("message", "")

    def redeem_page(self, voucher):
        return self.get(f"/redeem?voucher={voucher}").text

    def checkout(self):
        """Walk to the confirmation page (contact details step)."""
        r = self.get("/checkout/start")
        if "/checkout/questions/" not in r.url:
            raise ShopError(f"checkout did not start: {r.url}")
        r = self.post("/checkout/questions/", {"email": self.email})
        if "/checkout/confirm/" not in r.url:
            raise ShopError(f"questions step did not advance: {r.url}")
        return r

    def confirm(self):
        """Click "Place binding order"; returns (order code or None, message, seconds until the answer)."""
        t0 = time.time()
        r = self.post("/checkout/confirm/", {"ajax": "1"})
        result = self._wait_async(r.json(), timeout=120)
        m = ORDER_URL_RE.search(result.get("redirect") or "")
        if m:
            self.s.get(self.host + result["redirect"], timeout=60)
        return (m.group(1) if m else None), result.get("message", ""), time.time() - t0

    def join_waitinglist(self, item):
        r = self.get(f"/waitinglist?item={item}")
        if "/waitinglist" not in r.url:
            return False
        r = self.post(f"/waitinglist?item={item}", {"itemvar": str(item), "email": self.email})
        return r.status_code == 200 and "waitinglist" not in r.url.split("?")[0][-12:]


class ShopTraffic:
    """Background visitors: browse, register, abandon carts, queue on waiting lists, redeem vouchers."""

    def __init__(self, app, base, *, interval=(3.0, 9.0), abandon=0.12, join_waitlist=0.7, workers=3, exclude_items=()):
        self.app = app
        self.base = base
        self.workshops = list(app.catalog["workshops"])
        self.weights = {w["item"]: 1.0 / (1 + i * 0.25) for i, w in enumerate(self.workshops)}
        self.interval = interval
        self.abandon = abandon
        self.join_waitlist = join_waitlist
        self.workers = workers
        #: Items the background visitors leave alone (a problem may drive one itself).
        self.exclude_items = set(exclude_items)
        self.waitlisted: dict[str, int] = {}
        self.redeemed: set[str] = set()
        self.stats = {"orders": 0, "abandoned": 0, "waitlisted": 0, "redeemed": 0, "errors": 0, "refused": 0}
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._lock = threading.Lock()

    def visitor(self, email=None):
        return Visitor(self.base, self.app.catalog["organizer"], self.app.catalog["event"], email)

    # ------------------------------------------------------------------ visitors
    def _one_visit(self):
        v = self.visitor()
        v.visit()
        choices = [w for w in self.workshops if w["item"] not in self.exclude_items]
        if not choices:
            return
        item = random.choices([w["item"] for w in choices], [self.weights[w["item"]] for w in choices])[0]
        time.sleep(random.uniform(2, 10))
        ok, _ = v.add_to_cart(item)
        if not ok:
            self.stats["refused"] += 1
            if random.random() < self.join_waitlist and v.join_waitinglist(item):
                with self._lock:
                    self.waitlisted[v.email] = item
                self.stats["waitlisted"] += 1
            return
        if random.random() < self.abandon:
            self.stats["abandoned"] += 1
            return
        time.sleep(random.uniform(15, 90))
        v.checkout()
        time.sleep(random.uniform(4, 25))
        code, _, _ = v.confirm()
        if code:
            self.stats["orders"] += 1

    def _redeem_vouchers(self):
        """Waiting-list customers follow the link in their voucher email some minutes after it arrives."""
        try:
            found = self.app.mailbox('subject:"You have been selected from the waitinglist"', limit=200)
        except Exception:
            return
        due = []
        for message in found.get("messages", []):
            to = message["To"][0]["Address"]
            with self._lock:
                if to not in self.waitlisted or to in self.redeemed:
                    continue
            created = message.get("Created", "")
            due.append((to, message["ID"], created))
        for to, mid, _ in due:
            if random.random() > 0.5:
                continue  # read later
            with self._lock:
                self.redeemed.add(to)
            threading.Thread(target=self._redeem, args=(to, mid), daemon=True).start()

    def _redeem(self, email, message_id):
        try:
            time.sleep(random.uniform(20, 150))
            text = self.app.mail_text(message_id)
            m = VOUCHER_RE.search(text)
            if not m:
                return
            v = self.visitor(email)
            v.visit()
            v.redeem_page(m.group(1))
            ok, _ = v.add_to_cart(self.waitlisted[email], voucher=m.group(1))
            if not ok:
                return
            time.sleep(random.uniform(10, 60))
            v.checkout()
            time.sleep(random.uniform(3, 15))
            code, _, _ = v.confirm()
            if code:
                self.stats["redeemed"] += 1
        except Exception as exc:
            self.stats["errors"] += 1
            logger.debug("voucher redemption failed: %s", exc)

    def _visitor_loop(self):
        while not self._stop.is_set():
            try:
                self._one_visit()
            except Exception as exc:
                self.stats["errors"] += 1
                logger.debug("visit failed: %s", exc)
            self._stop.wait(random.uniform(*self.interval) * self.workers)

    def _mail_loop(self):
        while not self._stop.is_set():
            self._redeem_vouchers()
            self._stop.wait(30)

    def start(self):
        self._stop.clear()
        self._threads = [threading.Thread(target=self._visitor_loop, daemon=True) for _ in range(self.workers)]
        self._threads.append(threading.Thread(target=self._mail_loop, daemon=True))
        for t in self._threads:
            t.start()

    def stop(self, timeout=5):
        self._stop.set()
        for t in self._threads:
            t.join(timeout)
        self._threads = []
