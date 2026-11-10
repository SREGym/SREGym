"""Attendees on the event day, driven from the conductor host.

Registered attendees open their order page and download their ticket (the PDF with the
QR code), receive ticket emails, and at the entrance present the ticket they hold to the
handheld readers, which post the scanned barcode to the door-scanner station. The event
team's help desk re-issues tickets on request through the REST API (new ticket secret,
then the order email with the ticket is sent again). Everything here runs outside the
cluster and reaches pretix through the NodePort front door and the station's reader port.
"""

from __future__ import annotations

import base64
import json
import logging
import random
import threading
import time
import urllib.request

import requests

from sregym.service.apps.pretix.shop import USER_AGENTS

logger = logging.getLogger("all.sregym.apps.pretix.attendees")

#: Reads the ticket secrets printed on PDF tickets (pypdf ships with pretix); runs in pretix-web.
EXTRACT_SOURCE = r"""
import base64, io, re
from pypdf import PdfReader
out = []
for b in ARGS["pdfs"]:
    try:
        t = "".join(p.extract_text() or "" for p in PdfReader(io.BytesIO(base64.b64decode(b))).pages)
        found = set(re.findall(r"(?<![A-Za-z0-9])[a-z0-9]{%d}(?![A-Za-z0-9])" % ARGS["length"], t))
        out.append(sorted(found))
    except Exception:
        out.append([])
print(json.dumps(out))
"""


class TicketHolder:
    def __init__(self, order):
        self.code = order["code"]
        self.order_secret = order["secret"]
        self.email = order["email"]
        position = order["positions"][0]
        self.position = position["id"]
        self.item = position["item"]
        self.secret = position["secret"]  # as issued (tracked by the desk)
        self.held = None  # the secret printed on the ticket this attendee carries
        self.admitted = False
        self.rejections = 0
        self.s = requests.Session()
        self.s.headers["User-Agent"] = random.choice(USER_AGENTS)
        self.s.headers["Accept-Language"] = "en-US,en;q=0.9"


class EventDay:
    """Background attendee activity for one event day."""

    def __init__(
        self,
        app,
        base,
        scanner,
        token,
        holders,
        *,
        arrivals=(25.0, 60.0),
        downloads=(15.0, 40.0),
        desk=(150.0, 300.0),
        secret_length=32,
    ):
        self.app = app
        self.base = base
        self.shop = f"{base}/{app.catalog['organizer']}/{app.catalog['event']}"
        self.api = f"{base}/api/v1/organizers/{app.catalog['organizer']}/events/{app.catalog['event']}"
        self.scanner = scanner
        self.token = token
        self.holders = holders
        self.arrivals = arrivals
        self.downloads = downloads
        self.desk = desk
        self.secret_length = secret_length
        #: Attendees who arrive later in the day (not before the problem lets them).
        self.not_yet = set()
        #: Attendees the help desk is not asked about again.
        self.desk_skip = set()
        #: Attendees walking up to the entrance next (in this order).
        self.due = []
        #: Every re-issue the desk made: (order code, position id, old secret, new secret, time).
        self.reissues = []
        self.scans = []
        self.stats = {"downloads": 0, "admitted": 0, "rejected": 0, "reissued": 0, "errors": 0}
        self._stop = threading.Event()
        self._threads = []
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ primitives
    def call(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            self.api + path, data=data, method=method, headers={"Authorization": f"Token {self.token}"}
        )
        if data:
            req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=90) as r:
            body = r.read()
            return json.loads(body) if body else None

    def api_download(self, position, timeout=90):
        """The REST API's ticket download (409 while the ticket is being rendered)."""
        import urllib.error

        deadline = time.monotonic() + timeout
        while True:
            req = urllib.request.Request(
                f"{self.api}/orderpositions/{position}/download/pdf/", headers={"Authorization": f"Token {self.token}"}
            )
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    return r.read()
            except urllib.error.HTTPError as exc:
                if exc.code != 409 or time.monotonic() > deadline:
                    raise
            time.sleep(0.5)

    def secrets_on(self, pdfs):
        """The ticket secrets printed on each PDF."""
        if not pdfs:
            return []
        return self.app.pretix_python(
            EXTRACT_SOURCE, {"pdfs": [base64.b64encode(p).decode() for p in pdfs], "length": self.secret_length}
        )

    def held_secret(self, pdf):
        found = self.secrets_on([pdf])[0] if pdf else []
        return found[0] if len(found) == 1 else (found[0] if found else None)

    def start_download(self, h, kind="all"):
        """Click the ticket download button; returns the async status (the PDF is rendered on demand)."""
        path = f"/order/{h.code}/{h.order_secret}/download/" + ("pdf" if kind == "all" else f"{h.position}/pdf")
        r = h.s.get(self.shop + path, params={"ajax": "1"}, timeout=60)
        r.raise_for_status()
        return r.json()

    def finish_download(self, h, status, timeout=90):
        deadline = time.monotonic() + timeout
        while not status.get("ready"):
            if time.monotonic() > deadline:
                raise TimeoutError("ticket was not rendered")
            time.sleep(0.5)
            status = h.s.get(self.base + status["check_url"], timeout=60).json()
        url = status["redirect"]
        r = h.s.get(url if url.startswith("http") else self.base + url, timeout=60)
        if r.headers.get("Content-Type", "").startswith("application/pdf"):
            return r.content
        raise RuntimeError(f"download returned {r.status_code} {r.headers.get('Content-Type')}")

    def download(self, h, kind="all"):
        pdf = self.finish_download(h, self.start_download(h, kind))
        self.stats["downloads"] += 1
        return pdf

    def mail_attachment(self, h, subject_word="order", after=0.0, timeout=60):
        """The ticket attached to the newest mail to this attendee whose subject contains ``subject_word``."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            found = self.app.mailbox(f"to:{h.email}", limit=20).get("messages", [])
            for m in found:
                created = m.get("Created", "")
                if subject_word.lower() not in m.get("Subject", "").lower() or not m.get("Attachments"):
                    continue
                if after and _epoch(created) < after - 2:
                    continue
                full = json.loads(
                    self.app.command(
                        "exec",
                        "deploy/mailpit",
                        "--",
                        "wget",
                        "-qO-",
                        f"http://127.0.0.1:8025/api/v1/message/{m['ID']}",
                        timeout=60,
                    )
                )
                for a in full.get("Attachments", []):
                    if a.get("ContentType") == "application/pdf" or a.get("FileName", "").endswith(".pdf"):
                        data = self.app.command(
                            "exec",
                            "deploy/mailpit",
                            "--",
                            "sh",
                            "-c",
                            f"wget -qO- http://127.0.0.1:8025/api/v1/message/{m['ID']}/part/{a['PartID']} | base64 -w0",
                            timeout=60,
                        )
                        return base64.b64decode(data.strip())
            time.sleep(2)
        return None

    def arrive(self, h):
        """Show the ticket at the entrance: a handheld reader scans its QR code."""
        r = requests.post(f"{self.scanner}/scan", json={"barcode": h.held}, timeout=30)
        result = r.json()
        with self._lock:
            self.scans.append({"t": time.time(), "order": h.code, "barcode": h.held, **result})
        if result.get("status") == "ok":
            h.admitted = True
            self.stats["admitted"] += 1
        else:
            h.rejections += 1
            self.stats["rejected"] += 1
        return result

    def reissue(self, h):
        """Help desk: issue a new ticket secret for the attendee and send the order email with the ticket again."""
        old = h.secret
        pos = self.call("POST", f"/orderpositions/{h.position}/regenerate_secrets/", {})
        h.secret = pos["secret"]
        return old, pos["secret"]

    def resend(self, h):
        self.call("POST", f"/orders/{h.code}/resend_link/", {})

    def record_reissue(self, h, old, new):
        with self._lock:
            self.reissues.append({"order": h.code, "position": h.position, "old": old, "new": new, "t": time.time()})
        self.stats["reissued"] += 1

    def desk_reissue(self, h):
        """An ordinary re-issue: new secret, mail with the ticket again, the attendee keeps the mailed ticket."""
        t = time.time()
        old, new = self.reissue(h)
        self.record_reissue(h, old, new)
        time.sleep(random.uniform(2, 8))
        self.resend(h)
        pdf = self.mail_attachment(h, after=t)
        if pdf:
            h.held = self.held_secret(pdf) or h.held

    # ------------------------------------------------------------------ loops
    def _candidates(self, pred):
        with self._lock:
            return [h for h in self.holders.values() if h.code not in self.not_yet and pred(h)]

    def _download_loop(self):
        while not self._stop.is_set():
            self._stop.wait(random.uniform(*self.downloads))
            try:
                pool = self._candidates(lambda h: not h.admitted)
                if pool:
                    h = random.choice(pool)
                    pdf = self.download(h, random.choice(("all", "all", "position")))
                    h.held = self.held_secret(pdf) or h.held
            except Exception as exc:
                self.stats["errors"] += 1
                logger.debug("download failed: %s", exc)

    def _arrival_loop(self):
        while not self._stop.is_set():
            self._stop.wait(random.uniform(*self.arrivals))
            try:
                pool = self._candidates(lambda h: h.held and not h.admitted and h.rejections == 0)
                retry = self._candidates(lambda h: h.held and not h.admitted and 0 < h.rejections < 3)
                with self._lock:
                    nxt = self.due.pop(0) if self.due else None
                if nxt:
                    self.arrive(self.holders[nxt])
                elif retry and random.random() < 0.3:
                    h = random.choice(retry)
                    # Turned away once: pulls the ticket up again from the order page and tries again.
                    pdf = self.download(h, "all")
                    h.held = self.held_secret(pdf) or h.held
                    self.arrive(h)
                elif pool:
                    self.arrive(random.choice(pool))
            except Exception as exc:
                self.stats["errors"] += 1
                logger.debug("arrival failed: %s", exc)

    def _desk_loop(self):
        while not self._stop.is_set():
            self._stop.wait(random.uniform(*self.desk))
            if self._stop.is_set():
                break
            try:
                pool = self._candidates(lambda h: not h.admitted and h.held and h.code not in self.desk_skip)
                if pool:
                    self.desk_reissue(random.choice(pool))
            except Exception as exc:
                self.stats["errors"] += 1
                logger.debug("desk re-issue failed: %s", exc)

    def start(self, desk=True):
        self._stop.clear()
        loops = [self._download_loop, self._arrival_loop] + ([self._desk_loop] if desk else [])
        self._threads = [threading.Thread(target=f, daemon=True) for f in loops]
        for t in self._threads:
            t.start()

    def stop(self, timeout=10):
        self._stop.set()
        for t in self._threads:
            t.join(timeout)
        self._threads = []


def _epoch(iso):
    from datetime import datetime

    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0
