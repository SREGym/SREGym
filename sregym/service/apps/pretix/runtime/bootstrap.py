# Executed by PretixApplication.pretix_python, whose prelude imports json and defines ARGS.
# ruff: noqa: F821
import http.client
import json as _json

from django_scopes import scopes_disabled
from pretix.base.models import Event, Organizer, Team, User

with scopes_disabled():
    orga, _ = Organizer.objects.get_or_create(slug=ARGS["organizer"], defaults={"name": "Lakeside Tech"})
    admin, created = User.objects.get_or_create(
        email="events@lakeside-techday.test", defaults={"fullname": "Event team"}
    )
    if created:
        admin.set_password(ARGS["admin_password"])
        admin.save()
    team, _ = Team.objects.get_or_create(
        organizer=orga,
        name="Event team",
        defaults={"all_events": True, "all_event_permissions": True, "all_organizer_permissions": True},
    )
    team.members.add(admin)
    door, _ = Team.objects.get_or_create(
        organizer=orga,
        name="Door staff",
        defaults={
            "all_events": True,
            "limit_event_permissions": {"event.orders:read": True, "event.orders:checkin": True},
        },
    )
    organizer_token = (team.tokens.filter(name="Back office").first() or team.tokens.create(name="Back office")).token
    scanner_token = (door.tokens.filter(name="Door scanners").first() or door.tokens.create(name="Door scanners")).token

api = f"/api/v1/organizers/{ARGS['organizer']}"


def request(method, path, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", 8080, timeout=60)
    headers = {"Host": "pretix-web", "Authorization": f"Token {organizer_token}"}
    if body is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(body)
    conn.request(method, api + path, body=body, headers=headers)
    r = conn.getresponse()
    data = r.read()
    conn.close()
    return r.status, data


def call(method, path, json=None):
    status, data = request(method, path, json)
    if status >= 400:
        raise RuntimeError(f"{method} {path}: {status} {data[:500]}")
    return _json.loads(data) if data else None


ev = f"/events/{ARGS['event']}"
with scopes_disabled():
    exists = Event.objects.filter(organizer=orga, slug=ARGS["event"]).exists()
if not exists:
    call(
        "POST",
        "/events/",
        json={
            "name": {"en": "Lakeside Tech Day 2026"},
            "slug": ARGS["event"],
            "live": False,
            "currency": "EUR",
            "date_from": ARGS["date_from"],
            "date_to": ARGS["date_to"],
            "presale_start": ARGS["presale_start"],
            "presale_end": ARGS["date_to"],
            "is_public": True,
            "location": {"en": "Lakeside Conference Centre"},
            "timezone": "UTC",
        },
    )
    call(
        "PATCH",
        ev + "/settings/",
        json={
            "locales": ["en"],
            "locale": "en",
            "waiting_list_enabled": True,
            "waiting_list_auto": True,
            "attendee_names_asked": False,
            "max_items_per_order": 1,
        },
    )
    category = call("POST", ev + "/categories/", json={"name": {"en": "Workshops"}, "position": 0})
    for position, (title, room, seats) in enumerate(ARGS["workshops"]):
        item = call(
            "POST",
            ev + "/items/",
            json={
                "name": {"en": f"Workshop: {title}"},
                "description": {"en": f"{room}, limited to {seats} participants. Registration is free."},
                "category": category["id"],
                "default_price": "0.00",
                "admission": True,
                "active": True,
                "position": position,
                "max_per_order": 1,
                "allow_waitinglist": True,
            },
        )
        call("POST", ev + "/quotas/", json={"name": title, "size": seats, "items": [item["id"]], "variations": []})
        call(
            "POST",
            ev + "/checkinlists/",
            json={"name": f"{room}: {title}", "all_products": False, "limit_products": [item["id"]]},
        )
    call("POST", ev + "/checkinlists/", json={"name": "Main entrance", "all_products": True})
    call("PATCH", ev + "/", json={"live": True})

if ARGS.get("ticket_download"):
    with scopes_disabled():
        event = Event.objects.get(organizer=orga, slug=ARGS["event"])
        plugins = [p for p in (event.plugins or "").split(",") if p]
        if "pretix.plugins.ticketoutputpdf" not in plugins:
            event.plugins = ",".join(plugins + ["pretix.plugins.ticketoutputpdf"])
            event.save(update_fields=["plugins"])
        event.settings.ticketoutput_pdf__enabled = True
        event.settings.ticket_download = True
        event.settings.mail_attach_tickets = True
if ARGS.get("reservation_minutes"):
    call("PATCH", ev + "/settings/", json={"reservation_time": int(ARGS["reservation_minutes"])})
if ARGS.get("close_when_sold_out"):
    for q in call("GET", ev + "/quotas/?page_size=100")["results"]:
        if not q["close_when_sold_out"]:
            call("PATCH", ev + f"/quotas/{q['id']}/", json={"close_when_sold_out": True})

items = {i["id"]: i for i in call("GET", ev + "/items/?page_size=100")["results"]}
quotas = call("GET", ev + "/quotas/?page_size=100")["results"]
lists = call("GET", ev + "/checkinlists/?page_size=100")["results"]
workshops = []
rooms = []
for title, room, seats in ARGS["workshops"]:
    item = next(i for i in items.values() if i["name"]["en"] == f"Workshop: {title}")
    quota = next(q for q in quotas if item["id"] in q["items"])
    lst = next(cl for cl in lists if cl["limit_products"] == [item["id"]])
    workshops.append(
        {"title": title, "room": room, "seats": seats, "item": item["id"], "quota": quota["id"], "list": lst["id"]}
    )
    rooms.append({"id": lst["id"], "room": room, "workshop": title, "seats": seats})
entrance = next(cl["id"] for cl in lists if cl["all_products"])
print(
    json.dumps(
        {
            "organizer_token": organizer_token,
            "scanner_token": scanner_token,
            "workshops": workshops,
            "rooms": rooms,
            "entrance_list": entrance,
        }
    )
)
