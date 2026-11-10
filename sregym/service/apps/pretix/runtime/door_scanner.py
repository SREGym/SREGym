import json
import logging
import os
import random
import threading
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("door")

with open(os.environ["SCANNER_CONFIG"]) as fh:
    config = json.load(fh)
token = os.environ["PRETIX_TOKEN"]
base = f"{config['api']}/api/v1/organizers/{config['organizer']}"
event = f"{base}/events/{config['event']}"


def call(method, url, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Token {token}")
    req.add_header("User-Agent", "door-scanner/2.3")
    if data:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read() or b"null")


def waiting_at_entrance():
    url = f"{event}/checkinlists/{config['entrance']}/positions/?has_checkin=false&page_size=50"
    return call("GET", url)["results"]


def scan(secret):
    try:
        result = call(
            "POST", f"{base}/checkinrpc/redeem/", {"secret": secret, "lists": [config["entrance"]], "type": "entry"}
        )
        return result.get("status"), result.get("reason"), result.get("position")
    except urllib.error.HTTPError as exc:
        body = json.loads(exc.read() or b"{}")
        return body.get("status", exc.code), body.get("reason"), body.get("position")


class Readers(BaseHTTPRequestHandler):
    """Handheld readers at the entrance post each scanned barcode here."""

    def do_POST(self):
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            barcode = str(body.get("barcode", "")).strip()
            status, reason, position = scan(barcode)
            if status == "ok":
                log.info("entrance: ticket %s-%s admitted", position["order"], position["positionid"])
            elif position:
                log.warning("entrance: ticket %s-%s rejected (%s)", position["order"], position["positionid"], reason)
            else:
                log.warning("entrance: barcode %s rejected (%s)", barcode, reason)
            out, code = {"status": status, "reason": reason}, 200
        except Exception as exc:
            log.error("scanner error: %s", exc)
            out, code = {"status": "error", "reason": "station"}, 503
        data = json.dumps(out).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


if config.get("handheld_port"):
    server = ThreadingHTTPServer(("0.0.0.0", config["handheld_port"]), Readers)
    threading.Thread(target=server.serve_forever, daemon=True).start()


def waiting_for(room):
    """People on the room's waiting list: still waiting, and already sent a voucher."""
    url = f"{event}/waitinglistentries/?item={room['item']}&page_size=200"
    entries = call("GET", url)["results"]
    return [e for e in entries if not e["voucher"]], [e for e in entries if e["voucher"]]


seen = {}
last_warned = {}
next_status = 0
while True:
    try:
        now = time.time()
        if now >= next_status:
            next_status = now + 60
            for room in config["rooms"]:
                status = call("GET", f"{event}/checkinlists/{room['list']}/status/")
                tickets = status["position_count"]
                inside = status["checkin_count"]
                watch = config.get("waitlist_watch") and room.get("item")
                waiting, offered = waiting_for(room) if watch else ([], [])
                if seen.get(room["list"]) != (tickets, inside, len(waiting)):
                    seen[room["list"]] = (tickets, inside, len(waiting))
                    if watch:
                        log.info(
                            "%s (%s): %d tickets, %d checked in, %d seats, %d on the waiting list",
                            room["room"],
                            room["workshop"],
                            tickets,
                            inside,
                            room["seats"],
                            len(waiting),
                        )
                    else:
                        log.info(
                            "%s (%s): %d tickets, %d checked in, %d seats",
                            room["room"],
                            room["workshop"],
                            tickets,
                            inside,
                            room["seats"],
                        )
                if tickets > room["seats"] and now - last_warned.get(room["list"], 0) >= 300:
                    last_warned[room["list"]] = now
                    log.warning(
                        "%s (%s): %d valid tickets for %d seats", room["room"], room["workshop"], tickets, room["seats"]
                    )
                # Seats not yet offered to anyone (a waiting-list voucher holds its seat until it expires).
                if waiting and tickets + len(offered) < room["seats"]:
                    oldest = min(datetime.fromisoformat(w["created"].replace("Z", "+00:00")) for w in waiting)
                    minutes = (datetime.now(UTC) - oldest).total_seconds() / 60
                    if minutes >= 20 and now - last_warned.get(("waiting", room["list"]), 0) >= 300:
                        last_warned[("waiting", room["list"])] = now
                        log.warning(
                            "%s (%s): %d of %d seats free, %d waiting for a seat (longest %d min)",
                            room["room"],
                            room["workshop"],
                            room["seats"] - tickets - len(offered),
                            room["seats"],
                            len(waiting),
                            minutes,
                        )
        if config.get("walk_in", True):
            queue = waiting_at_entrance()
            if queue:
                position = random.choice(queue)
                status, reason, _ = scan(position["secret"])
                if status == "ok":
                    log.info("entrance: ticket %s admitted", position["order"])
                else:
                    log.warning("entrance: ticket %s rejected (%s)", position["order"], reason)
    except Exception as exc:
        log.error("scanner error: %s", exc)
    time.sleep(random.uniform(8, 20))
