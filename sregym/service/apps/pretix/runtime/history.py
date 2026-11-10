# Executed by PretixApplication.pretix_python, whose prelude imports json and defines ARGS.
# ruff: noqa: F821
import random
from datetime import timedelta
from decimal import Decimal

from django.db import transaction
from django.utils.timezone import now
from django_scopes import scope, scopes_disabled
from pretix.base.models import Event, Item, LogEntry, Order, OrderPayment, OrderPosition
from pretix.base.secrets import assign_ticket_secret

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
    "hugo",
    "ines",
    "karl",
    "olga",
    "pia",
    "raj",
    "tara",
    "yusuf",
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
    "haas",
    "lund",
    "okafor",
)
DOMAINS = ("example.org", "mail.test", "corp.test", "uni.test", "posteo.test", "lakeside-labs.test")

rng = random.Random(ARGS["seed"])
with scopes_disabled():
    ev = Event.objects.select_related("organizer").get(organizer__slug=ARGS["organizer"], slug=ARGS["event"])
made = {}
with scope(organizer=ev.organizer):
    channel = ev.organizer.sales_channels.get(identifier="web")
    presale_start = ev.presale_start or now() - timedelta(days=21)
    span = max(60, int((now() - timedelta(hours=2) - presale_start).total_seconds()))
    for item_id, count in ARGS["fills"].items():
        item = Item.objects.get(pk=int(item_id), event=ev)
        codes = []
        for _ in range(int(count)):
            placed = presale_start + timedelta(seconds=rng.randrange(span))
            first, last = rng.choice(FIRST), rng.choice(LAST)
            email = f"{first}.{last}{rng.choice(['', '', str(rng.randint(1, 99))])}@{rng.choice(DOMAINS)}"
            with transaction.atomic():
                order = Order(
                    event=ev,
                    status=Order.STATUS_PAID,
                    email=email,
                    locale="en",
                    total=Decimal("0.00"),
                    datetime=placed,
                    expires=placed + timedelta(days=14),
                    sales_channel=channel,
                )
                order.assign_code()
                order.save()
                Order.objects.filter(pk=order.pk).update(datetime=placed)
                pos = OrderPosition(order=order, item=item, price=Decimal("0.00"), positionid=1)
                assign_ticket_secret(ev, pos, save=False)
                pos.save()
                OrderPayment.objects.create(
                    order=order,
                    provider="free",
                    amount=Decimal("0.00"),
                    state=OrderPayment.PAYMENT_STATE_CONFIRMED,
                    payment_date=placed,
                )
                order.create_transactions()
                # Audit trail as the shop writes it (no notifications: these registrations are history).
                for offset, action, data in (
                    (0, "pretix.event.order.placed", {}),
                    (1, "pretix.event.order.paid", {"provider": "free", "info": None}),
                ):
                    entry = LogEntry(
                        content_object=order,
                        action_type=action,
                        event=ev,
                        organizer_id=ev.organizer_id,
                        data=json.dumps(data),
                    )
                    entry.save()
                    LogEntry.objects.filter(pk=entry.pk).update(datetime=placed + timedelta(seconds=offset))
            codes.append(order.code)
        made[item_id] = codes
print(json.dumps({"made": made}))
