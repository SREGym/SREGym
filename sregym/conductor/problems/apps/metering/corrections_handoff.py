"""Usage corrections lost across a make-before-break publication handoff on PostgreSQL 17.4.

Modelled on the PostgreSQL logical decoding defect fixed by commit 4909b38af034
(17.5): decoding did not pass catalog invalidations to transactions already in
progress, so a walsender could rebuild its publication cache under an older
snapshot and keep it. The company moves corrections (updates and deletes) from
each customer's usage publication to its corrections publication: enable them
on the corrections publication under load, wait until every slot confirms past
that change, and withdraw them from the usage publication overnight. Walsenders
that ran through the first step keep the corrections publication as it was
(publishing nothing) and load the withdrawal fresh, so they publish inserts
only until their own next restart. The same handoff a week earlier cost
finance's copy of billing's ledger its updates and deletes, and invoices went
out from it.

While four customer feeds dropped corrections, support's bulk correction
CF-31 and the patches it caused piled up: files of corrections for customers to
apply by hand, a usage row keyed in again, a refund, partial reverts (one of
which stopped Pell's feed on a duplicate key; on-call moved its slot forward
past everything sent meanwhile), a quantity fixed directly on core, nightly
correction files that Pell receives by subscription and loads automatically,
and two readers paused with a backlog (Larkspur's month-end copy, which stops on
its first error, during its close; Vale during its site maintenance). Every
server has since been patched to 17.6; only the damage and the patches remain.
"""

import contextlib
import json
import random
import subprocess
import time

from sregym.conductor.oracles.apps.metering.corrections_handoff import (
    MeteringOracle,
    copy_digest,
    documents,
    finance_differences,
    ledger,
)
from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.problems.base import Problem
from sregym.service.apps.metering.application import (
    CUSTOMER_DATABASES,
    NEW_IMAGE,
    STREAMED,
    TENANTS,
    MeteringApplication,
    ledger_conf,
    publications_conf,
    sql_literal,
)
from sregym.utils.decorators import mark_fault_injected

VICTIMS = ("pell_usage", "quill_usage", "larkspur_live", "ostrava_usage")
PAUSED = ("larkspur_monthend", "vale_usage")
STAFF = ("j.okafor", "m.tanaka", "r.lindqvist", "s.haddad", "a.moreau")
REASONS = {
    "void": (
        "duplicate meter flush after gateway retry",
        "customer sandbox traffic billed as production",
        "load run from our own staging key",
        "double count during gateway failover",
    ),
    "reduce": (
        "throttled calls billed at full count",
        "storage metered twice during volume migration",
        "token count included cached prompt",
        "partial outage credit on metered calls",
    ),
    "move": (
        "API key belongs to {to}",
        "subsidiary key reassigned to {to}",
        "usage reattributed to {to} per contract",
        "key transferred to {to} at reorganisation",
    ),
}
DECLARED_CORE = {t: ("insert", "update,delete") for t in STREAMED}
MAX_BUILDS = 3


class WorldDidNotTake(RuntimeError):
    """A handoff left no reader on the never-committed configuration; the world is rebuilt."""


class MeteringCorrectionsHandoff(Problem):
    cleanup_timeout_seconds = 1500
    run_default_workload = False

    def __init__(self):
        super().__init__(app=MeteringApplication())
        self.rng = random.Random()
        self.corrected = set()
        self.tickets = set()
        self.state = {}
        self.root_cause = self.build_structured_root_cause(
            component="logical decoding on the core and billing PostgreSQL servers (statefulset/core, "
            "statefulset/billing) during pubsync's corrections handoffs, while they ran PostgreSQL 17.4",
            namespace=self.namespace,
            description=(
                "PostgreSQL 17.4 logical decoding did not pass catalog invalidations to transactions already in "
                "progress (data loss bug fixed in 17.5, commit 4909b38af034). pubsync moved corrections (UPDATE and "
                "DELETE) with a make-before-break handoff: first ALTER PUBLICATION corr_<tenant> (and, a week "
                "earlier on billing, ledger_corr) SET (publish = 'update, delete'), then after every active slot "
                "confirmed past it, a quiet ALTER PUBLICATION usage_<tenant> (ledger_all) SET (publish = 'insert'). "
                "Each make step committed while an ingest batch (or billing apply transaction) that had already "
                "written was in flight; every walsender that decoded that transaction first cached the corrections "
                "publication's pre-change definition (publishing nothing) under its older snapshot. The later break "
                "was loaded fresh, so those walsenders published inserts only, a configuration never committed, "
                "until their own next restart. A walsender that restarted between the steps was cured only if its "
                "slot had already confirmed past the in-flight batch; one that started before that position decoded "
                "the batch again and cached the same stale definition. Affected: finance's subscription "
                "finance_ledger to billing (week one, until finance's subscription was disabled and enabled during "
                "maintenance; it missed ledger re-ratings, voids and moves, and invoices and support credit notes "
                "were issued from that ledger), and the customer subscriptions pell_usage, quill_usage and "
                "larkspur_live (from the break until each one's own restart) and ostrava_usage (its link dropped "
                "across the make and it reconnected before its slot passed the in-flight batch, so it stayed stale "
                "until the 17.6 restart). They kept voided and moved-out rows and stale quantities, and lacked "
                "moved-in rows, including support's bulk correction CF-31. larkspur_monthend and vale_usage were "
                "not stale (their walsenders restarted after their slots passed the batch); they are paused with a "
                "backlog at the customers' requests (month-end's subscription stops on its first error). billing "
                "was not affected. Gone since both servers were patched to 17.6, so only the damage remains, along "
                "with workarounds that are not the cause: support's correction files, the re-keyed usage row, the "
                "refund, the nightly correction files, on-call's direct UPDATE of a Quill quantity on core (no "
                "correction record), and on-call's pg_replication_slot_advance of Pell's slot after its feed "
                "stopped on a duplicate key (which also skipped every Pell change committed in between). Also not "
                "the cause: the collation version warnings (they started with the 17.6 image), the customers' "
                "databases, billing's rating, finance's closed-period rule (by design), the add-table race (pubsync "
                "locks the table), subscriber lag, the paused feeds, or a need to reconnect after publication changes."
            ),
        )
        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)
        self.mitigation_oracle = MeteringOracle(problem=self)

    # ------------------------------------------------------------ corrections
    def pick_event(self, tenant, invoiced=None):
        app = self.app
        exclude = ",".join(str(e) for e in self.corrected) or "0"
        if invoiced is None:
            event = app.core(
                f"select event_id from usage_events where tenant_id = {sql_literal(tenant)} "
                f"and event_id not in ({exclude}) and event_id > (select max(event_id) - 3000 from usage_events) "
                "order by random() limit 1;"
            ).strip()
        else:
            event = app.finance(
                f"select line_id from ledger_lines where account = {sql_literal(tenant)} "
                f"and invoice_id is {'not ' if invoiced else ''}null and line_id not in ({exclude}) "
                "and not exists (select 1 from ledger_adjustments a where a.line_id = ledger_lines.line_id) "
                "order by random() limit 1;"
            ).strip()
            if event and app.core(f"select count(*) from usage_events where event_id = {event};").strip() != "1":
                event = ""
        if not event:
            raise RuntimeError(f"no event to correct for {tenant} (invoiced={invoiced})")
        self.corrected.add(int(event))
        return int(event)

    def correction_value(self, event, kind, to=None):
        if kind == "reduce":
            quantity = float(self.app.core(f"select quantity from usage_events where event_id = {event};").strip())
            return f"{round(quantity * self.rng.choice((0.25, 0.4, 0.5, 0.6)), 3):.3f}"
        return to if kind == "move" else None

    def reason(self, kind, to=None):
        return self.rng.choice(REASONS[kind]).format(to=TENANTS.get(to, ("",))[0])

    def correct(self, tenant, kind, to=None, invoiced=None, event=None):
        if event is None:
            event = self.pick_event(tenant, invoiced)
        self.corrected.add(event)
        value = self.correction_value(event, kind, to)
        self.app.core(
            f"select correct_usage({event}, {sql_literal(kind)}, {'null' if value is None else sql_literal(value)}, "
            f"{sql_literal(self.reason(kind, to))}, {sql_literal(self.rng.choice(STAFF))});"
        )
        return event

    def corrections(self, plan, pause=(0.5, 2)):
        events = []
        for spec in plan:
            events.append(self.correct(*spec))
            time.sleep(self.rng.uniform(*pause))
        return events

    def random_plan(self, count, invoiced=None):
        tenants = list(TENANTS)
        plan = []
        for _ in range(count):
            tenant = self.rng.choice(tenants)
            kind = self.rng.choice(("void", "reduce", "reduce", "move"))
            to = self.rng.choice([t for t in tenants if t != tenant]) if kind == "move" else None
            plan.append((tenant, kind, to, invoiced))
        return plan

    def correct_batch(self, batch, items):
        """One transaction of several corrections through the correction tool; items: (event, kind, to)."""
        payload = [
            {"event": e, "kind": k, "value": self.correction_value(e, k, to), "reason": self.reason(k, to)}
            for e, k, to in items
        ]
        self.corrected.update(e for e, _k, _to in items)
        self.app.core(
            f"select correct_usage_batch({sql_literal(batch)}, {sql_literal(json.dumps(payload))}::jsonb, "
            f"{sql_literal(self.rng.choice(STAFF))});"
        )

    # ---------------------------------------------------------------- support
    def ticket(self, account, subject, notes=(), status="open"):
        while True:
            ticket = f"SUP-{self.rng.randint(4100, 4999)}"
            if ticket not in self.tickets:
                break
        self.tickets.add(ticket)
        self.app.core(
            f"insert into tickets (ticket_id, account, subject, status) values ({sql_literal(ticket)}, "
            f"{'null' if account is None else sql_literal(account)}, {sql_literal(subject)}, {sql_literal(status)});",
            database="support",
        )
        for author, body in notes:
            self.note(ticket, author, body)
        return ticket

    def note(self, ticket, author, body):
        self.app.core(
            f"insert into ticket_notes (ticket_id, author, body) values ({sql_literal(ticket)}, "
            f"{sql_literal(author)}, {sql_literal(body)});",
            database="support",
        )
        time.sleep(0.3)

    @staticmethod
    def customer_name(tenant):
        return f"{TENANTS[tenant][0]} (customer)"

    def staff(self):
        return self.rng.choice(STAFF)

    # ---------------------------------------------------------------- picking
    def events_of(self, tenant):
        return {
            int(e)
            for e in self.app.core(
                f"select event_id from usage_events where tenant_id = {sql_literal(tenant)};"
            ).split()
        }

    def issued(self, database):
        """Events present in a customer copy whose row is booked on its statement."""
        return {
            int(e)
            for e in self.app.customer(
                database,
                "select u.event_id from usage_events u join statement_lines s on s.line_id = u.line_id and s.kind = 'line';",
            ).split()
        }

    def choose(self, candidates, what):
        pool = sorted(set(candidates) - self.corrected)
        if not pool:
            raise RuntimeError(f"no event for {what}")
        event = self.rng.choice(pool)
        self.corrected.add(event)
        return event

    def corrections_rows(self, where):
        """Correction rows as the extract files carry them."""
        return json.loads(
            self.app.core(
                "select coalesce(jsonb_agg(jsonb_build_object('correction_id', correction_id, 'event_id', event_id, "
                "'kind', kind, 'before', before, 'after', after) order by correction_id), '[]') "
                f"from usage_corrections where {where};"
            )
        )

    def send_file(self, ticket, account, rows, status):
        self.app.core(
            f"insert into extract_files (ticket_id, account, status, rows) values ({sql_literal(ticket)}, "
            f"{sql_literal(account)}, {sql_literal(status)}, {sql_literal(json.dumps(rows))}::jsonb);",
            database="support",
        )

    # ---------------------------------------------------------------- history
    @contextlib.contextmanager
    def phase(self, name):
        start = time.monotonic()
        try:
            yield
        finally:
            print(f"phase.{name}.duration_s={time.monotonic() - start:.0f}", flush=True)

    def build(self):
        with self.phase("ordinary"):
            self.ordinary()
        with self.phase("week_one"):
            credits = self.week_one()
        with self.phase("week_two"):
            target = self.week_two()
        with self.phase("support_story"):
            facts = self.support_story()
        app = self.app
        app.invoice_run(["quill", "garnet", "hollis"], "invoice-run-close-4")
        app.capture_invoices()
        # Routine patching moves both servers to the current minor release.
        with self.phase("patch"):
            app.set_server("billing", NEW_IMAGE)
            app.set_server("core", NEW_IMAGE)
            time.sleep(20)
        return {"credits": len(credits), "target": target, **facts}

    def ordinary(self):
        app = self.app
        app.truncate_server_logs()
        app.pubsync(
            "core", publications_conf("r1", "initial feeds", {t: ("insert,update,delete", "") for t in STREAMED}), "r1"
        )
        app.pubsync("billing", ledger_conf("r1", "ledger feed for finance", "insert,update,delete", ""), "r1")
        app.subscribe_billing()
        app.subscribe_finance()
        app.subscribe_customers()
        lark = self.customer_name("larkspur")
        self.ticket(
            "larkspur",
            "Month-end database onboarding",
            [
                (
                    lark,
                    "Our month-end database must never take a partial close. We created its subscription to stop "
                    "on the first error; if it stops, our DBA reviews the close before re-enabling it, which "
                    "takes days.",
                ),
                (self.staff(), "Noted."),
            ],
            status="closed",
        )
        app.set_rate(60)
        time.sleep(75)

        # Months of ordinary operation, compressed: corrections flow to every reader; first invoices.
        self.corrections(self.random_plan(10))
        time.sleep(30)
        app.invoice_run(list(TENANTS), "invoice-run-close-1")
        app.capture_invoices()
        time.sleep(60)
        self.corrections(self.random_plan(8, invoiced=True))
        time.sleep(20)

    def week_one(self):
        app = self.app
        # Week one: billing's ledger corrections move to ledger_corr during billing's busy hour.
        app.set_rate(1500)
        time.sleep(25)
        app.start_rerate(f"billing-rerate-{int(time.time() // 60)}")
        app.pubsync(
            "billing",
            ledger_conf(
                "r2",
                "finance corrections: enable updates and deletes on ledger_corr (1/2)",
                "insert,update,delete",
                "update,delete",
            ),
            "r2",
        )
        time.sleep(10)
        self.corrections(self.random_plan(4))
        app.set_rate(0)
        app.wait_inbox_empty()
        app.wait_caught_up()
        app.wait_caught_up(app.billing)
        time.sleep(10)
        app.pubsync(
            "billing",
            ledger_conf(
                "r3",
                "finance corrections: withdraw updates and deletes from ledger_all (2/2)",
                "insert",
                "update,delete",
            ),
            "r3",
        )
        app.set_rate(60)
        time.sleep(20)
        week1 = self.corrections(
            [(t, "void", None, True) for t in ("pell", "arden", "cinder", "garnet")]
            + [(t, "void", None, False) for t in ("pell", "larkspur", "arden", "quill", "hollis")]
            + [(t, "reduce", None, True) for t in ("larkspur", "brisk", "ostrava")]
            + [(t, "reduce", None, False) for t in ("garnet", "vale", "dorian", "pell")]
            + [("cinder", "move", "ember", True), ("arden", "move", "pell", False), ("ionic", "move", "fallow", False)]
            + self.random_plan(5)
        )
        time.sleep(20)
        missed = {line for line, _account in finance_differences(app)} & set(week1)
        if not missed:
            raise WorldDidNotTake("finance received every week-one ledger change")
        first_half = ["pell", "arden", "cinder", "garnet", "brisk", "larkspur", "ionic"]
        app.invoice_run(first_half, "invoice-run-close-2")
        app.capture_invoices()
        credits = self.issue_credits(week1)
        time.sleep(20)
        # Finance's DBA maintenance: its subscription to billing is disabled and enabled again.
        app.bounce_finance()
        time.sleep(30)
        self.corrections(self.random_plan(6))
        time.sleep(20)
        app.invoice_run([t for t in TENANTS if t not in first_half], "invoice-run-close-3")
        app.capture_invoices()
        time.sleep(20)
        return credits

    def week_two(self):
        app = self.app
        # Week two: the same handoff for every opted-in customer on core, during ingest's busy hour.
        app.set_rate(1500)
        time.sleep(25)
        # Ostrava's link drops just before the first step and is back right after it, before its slot has
        # confirmed past the ingest batch in flight at the step.
        app.customer("ostrava_usage", "alter subscription ostrava_usage disable;")
        app.wait_slot("ostrava_usage", False)
        app.pubsync(
            "core",
            publications_conf(
                "r2",
                "customer corrections: enable updates and deletes on corr_* (1/2)",
                {t: ("insert,update,delete", "update,delete") for t in STREAMED},
            ),
            "r2",
        )
        app.customer("ostrava_usage", "alter subscription ostrava_usage enable;")
        app.wait_slot("ostrava_usage", True)
        app.set_rate(150)
        target = app.core("select pg_current_wal_lsn();").strip()
        time.sleep(15)
        # Month-end's and Vale's links drop and reconnect after their slots confirmed past that batch.
        survivors = ("larkspur_monthend", "vale_usage")
        app.wait_caught_up(slots=list(survivors))
        for db in survivors:
            app.restart_subscription(db)
            time.sleep(self.rng.uniform(5, 15))
        self.corrections(self.random_plan(5))
        time.sleep(15)
        app.set_rate(0)
        app.wait_inbox_empty()
        app.wait_caught_up()
        app.wait_caught_up(app.billing)
        time.sleep(10)
        app.pubsync(
            "core",
            publications_conf(
                "r3", "customer corrections: withdraw updates and deletes from usage_* (2/2)", DECLARED_CORE
            ),
            "r3",
        )
        app.set_rate(60)
        time.sleep(30)
        week2 = self.corrections(
            [
                ("pell", "void", None, None),
                ("pell", "move", "garnet", None),
                ("larkspur", "void", None, None),
                ("arden", "move", "larkspur", None),
                ("larkspur", "move", "hollis", None),
                ("quill", "reduce", None, None),
                ("quill", "void", None, None),
                ("pell", "reduce", None, None),
                ("cinder", "move", "quill", None),
                ("ostrava", "void", None, None),
                ("ostrava", "reduce", None, None),
                ("vale", "move", "pell", None),
                ("larkspur", "reduce", None, None),
                ("pell", "void", None, True),
                ("larkspur", "void", None, True),
                ("quill", "reduce", None, True),
                ("garnet", "move", "larkspur", True),
            ]
            + self.random_plan(4)
        )
        time.sleep(20)
        for db in VICTIMS:
            digest = copy_digest(app, db, CUSTOMER_DATABASES[db])
            if not set(week2) & {e for kind in digest.values() for e in kind}:
                raise WorldDidNotTake(f"{db} received every week-two correction")
        return target

    def support_story(self):
        """Support's bulk correction CF-31 and the patches it caused, while four feeds still drop corrections."""
        app = self.app
        lark, pell = self.customer_name("larkspur"), self.customer_name("pell")

        # Every customer closes its statement once.
        for db in CUSTOMER_DATABASES:
            app.close_statement(db)

        # Vale's planned site maintenance: Vale takes its own database offline, in a quiet minute.
        self.ticket(
            "vale",
            "Planned site maintenance",
            [
                (
                    self.customer_name("vale"),
                    "We are taking our site offline for planned maintenance and will be back on Saturday. "
                    "Our usage feed should catch up once we are back.",
                ),
                (self.staff(), "Thanks, noted on our side."),
            ],
        )
        app.set_rate(0)
        app.wait_inbox_empty()
        app.wait_caught_up(slots=["vale_usage"])
        app.customer("vale_usage", "alter subscription vale_usage disable;")
        app.wait_slot("vale_usage", False)

        # Larkspur's month-end close: on-call pauses the month-end feed at Larkspur's request, during a busy minute.
        app.close_statement("larkspur_monthend")
        month_end = self.ticket(
            "larkspur",
            "Pause month-end feed during close",
            [
                (
                    lark,
                    "We are starting our month-end close. Please pause the usage feed into our month-end "
                    "database until we confirm the close is done.",
                )
            ],
        )
        app.set_rate(1500)
        time.sleep(12)
        app.pause_reader("larkspur_monthend")
        app.set_rate(60)
        self.note(month_end, f"on-call: {self.staff()}", "Paused the month-end feed for Larkspur.")
        time.sleep(5)

        # CF-31: the account review's corrections, one batch, under ingest load.
        larkspur = self.events_of("larkspur")
        both = larkspur & self.issued("larkspur_live") & self.issued("larkspur_monthend")
        x = self.choose(both, "X")
        z = self.choose(both, "Z")
        w = self.choose(self.events_of("brisk"), "W")
        v1 = self.choose(self.events_of("vale") & self.issued("vale_usage"), "V1")
        pell_rows = self.events_of("pell") & self.issued("pell_usage")
        invoiced = {
            int(e)
            for e in app.finance(
                "select l.line_id from ledger_lines l join invoice_items i on i.line_id = l.line_id and i.kind = 'usage' "
                "where l.account = 'pell' and l.invoice_id is not null and i.amount >= 1 "
                "and not exists (select 1 from ledger_adjustments a where a.line_id = l.line_id);"
            ).split()
        }
        p = self.choose(pell_rows & invoiced, "P")
        pell_voids = [self.choose(pell_rows, "Pell void") for _ in range(2)]
        y = self.choose(pell_rows - invoiced, "Y")
        moved_in = [
            self.choose(self.events_of("arden"), "move into Pell"),
            self.choose(self.events_of("cinder"), "move into Pell"),
        ]
        quill = self.choose(self.events_of("quill"), "Quill reduction")
        lsn_before = app.core("select pg_current_wal_lsn();").strip()
        self.correct_batch(
            "CF-31",
            [
                (x, "move", "garnet"),
                (z, "void", None),
                (w, "move", "larkspur"),
                (v1, "move", "ostrava"),
                (p, "move", "garnet"),
                (y, "move", "garnet"),
                *[(e, "void", None) for e in pell_voids],
                *[(e, "move", "pell") for e in moved_in],
                (quill, "reduce", None),
            ],
        )
        review = self.ticket(
            None,
            "Account review CF-31: usage corrections",
            [(self.staff(), "Applied the CF-31 corrections from the account review as one batch.")],
            status="closed",
        )
        time.sleep(5)

        # The corrections do not show in Pell's and Larkspur's copies: support sends them as files.
        pell_ticket = self.ticket(
            "pell",
            "Corrections missing from live usage copy",
            [
                (
                    pell,
                    "The corrections from your account review are not in our live usage database. "
                    "We still see usage you told us was voided or moved.",
                ),
                (
                    self.staff(),
                    "Sent the CF-31 corrections for your account as a file so you can apply them on your side "
                    "while we look into the feed.",
                ),
            ],
        )
        self.send_file(
            pell_ticket,
            "pell",
            self.corrections_rows(
                "batch = 'CF-31' and (before->>'tenant_id' = 'pell' or after->>'tenant_id' = 'pell')"
            ),
            "sent",
        )
        lark_ticket = self.ticket(
            "larkspur",
            "Corrections missing from usage copies",
            [
                (lark, "Some of the CF-31 corrections are not in our databases."),
                (
                    self.staff(),
                    "Sent the CF-31 corrections for your account as a file so you can apply them on your side.",
                ),
            ],
        )
        lark_rows = self.corrections_rows(
            "batch = 'CF-31' and (before->>'tenant_id' = 'larkspur' or after->>'tenant_id' = 'larkspur')"
        )
        self.send_file(lark_ticket, "larkspur", lark_rows, "sent")

        # Customer side: Pell's DBA applies the voids and some of the moves by hand, then closes its statement.
        pell_file = self.corrections_rows(
            f"batch = 'CF-31' and event_id in ({','.join(map(str, pell_voids + moved_in))})"
        )
        loaded = self.rng.choice([[moved_in[0]], [moved_in[1]], list(moved_in)])
        app.customer(
            "pell_usage",
            f"select load_correction_file({sql_literal(json.dumps([r for r in pell_file if r['event_id'] in pell_voids + loaded]))}::jsonb, 'pell');",
        )
        app.close_statement("pell_usage")
        self.note(pell_ticket, pell, "Our DBA worked through the file by hand.")
        # Larkspur's DBA may load the moved-in row into the paused month-end copy, then closes it.
        w_loaded = self.rng.random() < 0.5
        if w_loaded:
            app.customer(
                "larkspur_monthend",
                f"select load_correction_file({sql_literal(json.dumps([r for r in lark_rows if r['event_id'] == w]))}::jsonb, 'larkspur');",
            )
        app.close_statement("larkspur_monthend")
        self.note(lark_ticket, lark, "Thanks, our DBA will go through it.")

        # Quill reports a record counted twice; on-call halves it directly in the usage table.
        q_row = app.rows(
            app.core,
            "select json_build_object('event', event_id, 'record', record_id, 'quantity', quantity) from usage_events "
            f"where tenant_id = 'quill' and metric = 'api_calls' and quantity >= 2 "
            f"and event_id not in ({','.join(map(str, self.corrected))}) and event_id in "
            f"({','.join(map(str, self.issued('quill_usage')))}) order by random() limit 1;",
        )[0]
        q = self.choose([q_row["event"]], "Q")
        q_ticket = self.ticket(
            "quill",
            "Record counted twice",
            [(self.customer_name("quill"), f"Record {q_row['record']} counts our calls twice.")],
        )
        app.core(f"update usage_events set quantity = {round(float(q_row['quantity']) / 2, 3)} where event_id = {q};")
        self.note(q_ticket, self.staff(), "Fixed the quantity on our side.")

        # Nightly correction files until the feeds are fixed; Pell receives its files by subscription and
        # loads them automatically.
        self.correct("pell", "void")
        time.sleep(5)
        app.setup_extracts({"pell": pell_ticket, "larkspur": lark_ticket})
        app.subscribe_files("pell", "pell_usage")
        for ticket in (pell_ticket, lark_ticket):
            self.note(
                ticket,
                self.staff(),
                "Until the feed is fixed we will send you a file of corrections for your account every night.",
            )
        app.run_cronjob("correction-extracts", "correction-extracts-manual-1")
        app.load_extract_files("pell", "pell_usage")
        self.note(pell_ticket, pell, "Thanks, we load your nightly file automatically.")

        # An ordinary correction later reduces one of the rows moved into Pell.
        reduced = self.rng.choice(moved_in)
        self.correct("pell", "reduce", event=reduced)
        time.sleep(5)

        # Larkspur's live copy lacks the moved-in row: support keys it in again by hand.
        row = app.rows(
            app.core,
            f"select json_build_object('metric', metric, 'quantity', quantity, 'occurred_at', occurred_at) "
            f"from usage_events where event_id = {w};",
        )[0]
        missing_ticket = self.ticket(
            "larkspur",
            "Usage missing from live copy",
            [
                (
                    lark,
                    f"A {row['metric']} record from {row['occurred_at'][:16].replace('T', ' ')} that your corrections "
                    "file moved to our account is not in our live usage database.",
                )
            ],
        )
        w2 = int(
            app.core(
                f"select enter_usage('larkspur', {sql_literal(row['metric'])}, {row['quantity']}, "
                f"{sql_literal(row['occurred_at'])}::timestamptz, {sql_literal(missing_ticket)}, {sql_literal(self.staff())});"
            ).strip()
        )
        self.corrected.add(w2)
        self.note(missing_ticket, self.staff(), f"Entered the missing usage by hand as manual-{missing_ticket}.")

        # Pell disputes P: its copy still shows it, the file says it moved, and an invoice charged it.
        charge = app.rows(
            app.finance,
            f"select json_build_object('invoice', i.invoice_id, 'amount', round(i.amount, 2)) from invoice_items i "
            f"where i.kind = 'usage' and i.line_id = {p};",
        )[0]
        capture = app.core(
            f"select capture_id from captures where invoice_id = {charge['invoice']};", database="payments"
        ).strip()
        dispute = self.ticket(
            "pell",
            f"Disputed charge on invoice {charge['invoice']}",
            [
                (
                    pell,
                    f"Your corrections file says usage event {p} was moved to Garnet Travel, but invoice "
                    f"{charge['invoice']} still charges us for it.",
                )
            ],
        )
        memo = f"Dispute {dispute}: usage event {p} on invoice {charge['invoice']} belongs to Garnet Travel"
        app.core(
            f"insert into refunds (capture_id, amount, memo, issued_by) values ({capture}, {charge['amount']}, "
            f"{sql_literal(memo)}, {sql_literal(self.staff())});",
            database="payments",
        )
        self.note(
            dispute, self.staff(), f"Refunded {charge['amount']} on the card payment for invoice {charge['invoice']}."
        )

        # Two CF-31 moves are reverted after the account owners object.
        self.correct_batch("CF-31", [(x, "move", "larkspur"), (v1, "move", "vale")])
        self.note(
            review,
            self.staff(),
            f"Reverted two CF-31 moves after the account owners objected: event {x} back to "
            f"Larkspur, event {v1} back to Vale.",
        )
        time.sleep(5)
        app.wait_caught_up(slots=["ostrava_usage"])
        app.close_statement("ostrava_usage")
        self.note(
            month_end,
            lark,
            "Close is confirmed on our side and our DBA has re-enabled the month-end subscription. "
            "Please resume the month-end feed.",
        )
        app.customer("larkspur_monthend", "alter subscription larkspur_monthend enable;")

        # The customers' links drop and reconnect, one by one.
        app.restart_subscription("pell_usage")
        time.sleep(5)
        gap = self.pell_gap(review, y)
        time.sleep(10)
        app.restart_subscription("quill_usage")
        time.sleep(5)
        app.restart_subscription("larkspur_live")
        time.sleep(10)
        self.corrections([("pell", "reduce", None, None)] + self.random_plan(5))
        time.sleep(10)
        app.wait_caught_up(skip=PAUSED)
        app.run_cronjob("correction-extracts", "correction-extracts-manual-2")
        # The customers that are online close their statements as every day.
        for db in ("pell_usage", "quill_usage", "larkspur_live", "ostrava_usage"):
            app.close_statement(db)
        return {
            "x": x,
            "z": z,
            "w": w,
            "w2": w2,
            "duplicate_usage": {"tenant_id": "larkspur", **row},
            "v1": v1,
            "p": p,
            "y": y,
            "q": q,
            "q_quantity": str(q_row["quantity"]),
            "pell_voids": pell_voids,
            "moved_in": moved_in,
            "pell_loaded": loaded,
            "pell_reduced": reduced,
            "w_loaded": w_loaded,
            "lsn_before_cf31": lsn_before,
            "refund_capture": int(capture),
            **gap,
        }

    def pell_gap(self, review, y):
        """Support reverts Y to Pell; Pell never heard it leave, so its feed stops on Y. On-call skips the
        failing change by moving Pell's slot to the current position, which also skips everything sent since."""
        app = self.app
        pell = self.customer_name("pell")
        errors = app.customer(
            "pell_usage", "select apply_error_count from pg_stat_subscription_stats where subname = 'pell_usage';"
        ).strip()
        self.correct_batch("CF-31", [(y, "move", "pell")])
        self.note(review, self.staff(), f"Reverted event {y} back to Pell after the account owner objected.")
        deadline = time.monotonic() + 120
        while (
            app.customer(
                "pell_usage", "select apply_error_count from pg_stat_subscription_stats where subname = 'pell_usage';"
            ).strip()
            == errors
        ):
            if time.monotonic() > deadline:
                raise WorldDidNotTake("Pell's feed did not stop on the reverted move")
            time.sleep(1)
        stopped = time.monotonic()
        first = int(app.core("select max(event_id) from usage_events;").strip())
        failing = self.ticket(
            "pell",
            "Usage feed failing",
            [
                (
                    pell,
                    'Our feed is failing: duplicate key value violates unique constraint "usage_events_pkey", '
                    f"Key (event_id)=({y}) already exists.",
                )
            ],
        )
        # Ordinary work goes on meanwhile, including one reduction of a row Pell holds.
        self.corrections(
            [
                ("quill", "void", None, None),
                ("larkspur", "void", None, None),
                ("brisk", "move", "larkspur", None),
                ("quill", "move", "vale", None),
                ("larkspur", "reduce", None, True),
            ]
            + self.random_plan(3)
        )
        reduced = self.choose(self.events_of("pell") & self.issued("pell_usage"), "Pell reduction in the gap")
        self.correct("pell", "reduce", event=reduced)
        time.sleep(max(0.0, self.rng.uniform(60, 90) - (time.monotonic() - stopped)))
        app.pause_reader("pell_usage")
        skipped_to = app.core(
            "select end_lsn from pg_replication_slot_advance('pell_usage', pg_current_wal_lsn());"
        ).strip()
        app.core("alter role feed_pell_usage login;")
        last = int(app.core("select max(event_id) from usage_events;").strip())
        self.note(failing, f"on-call: {self.staff()}", "Skipped the failing change on our side; feed resumed.")
        app.wait_slot("pell_usage", True)
        return {"pell_gap": [first, last], "pell_gap_reduced": reduced, "pell_skipped_to": skipped_to}

    def issue_credits(self, week1_events):
        """Support credits the customers who complained about a voided line on an invoice."""
        app = self.app
        rows = app.rows(
            app.finance,
            (
                "select json_build_object('line', l.line_id, 'account', l.account, 'amount', round(l.amount, 2), "
                "'invoice', l.invoice_id) from ledger_lines l "
                f"where l.line_id in ({','.join(map(str, week1_events))}) and l.invoice_id is not null;"
            ),
        )
        voided = {
            int(e)
            for e in app.core(
                f"select string_agg(event_id::text, ',') from usage_corrections where kind = 'void' "
                f"and event_id in ({','.join(map(str, week1_events))});"
            )
            .strip()
            .split(",")
            if e
        }
        credits = []
        for account in ("pell", "arden"):
            line = next((r for r in rows if r["account"] == account and r["line"] in voided and r["amount"] > 0), None)
            if not line:
                continue
            ticket = self.ticket(
                account,
                f"Voided usage billed on invoice {line['invoice']}",
                [
                    (
                        self.customer_name(account),
                        f"Invoice {line['invoice']} charges usage your team told us was voided.",
                    ),
                    (self.staff(), f"Issued a credit note of {line['amount']} for the voided usage."),
                ],
                status="closed",
            )
            memo = f"Disputed usage on invoice {line['invoice']}: voided usage was billed ({ticket})"
            app.finance(
                f"insert into credit_notes (account, amount, memo, issued_by) values ({sql_literal(account)}, "
                f"{line['amount']}, {sql_literal(memo)}, {sql_literal(self.rng.choice(STAFF))});"
            )
            credits.append({**line, "ticket": ticket})
        return credits

    # ------------------------------------------------------------------- seal
    def seal(self, facts):
        """Assert the per-reader outcome the story needs; returns the facts the oracle keeps."""
        app = self.app
        app.set_rate(0)
        app.wait_inbox_empty()
        app.wait_caught_up(skip=PAUSED)
        app.wait_caught_up(app.billing)
        time.sleep(5)
        problems = []
        diffs = {db: copy_digest(app, db, tenant) for db, tenant in CUSTOMER_DATABASES.items()}
        for db in VICTIMS:
            if not any(diffs[db].values()):
                problems.append(f"{db} received every correction")
        if facts["p"] not in diffs["pell_usage"]["extra"]:
            problems.append("pell_usage does not hold P")
        if facts["w"] not in diffs["larkspur_live"]["missing"]:
            problems.append("larkspur_live has W")
        finance_gaps = len(finance_differences(app))
        if not finance_gaps:
            problems.append("finance received every ledger change")
        slots = app.slot_lsns()
        for db in PAUSED:
            slot = slots.get(db)
            if (
                not slot
                or slot["active"]
                or app.core(
                    f"select {sql_literal(slot['confirmed'])}::pg_lsn <= {sql_literal(facts['lsn_before_cf31'])}::pg_lsn;"
                ).strip()
                != "t"
            ):
                problems.append(f"{db} is not paused before CF-31: {slot}")
        problems += self.seal_readers(facts, diffs, slots)
        held = {
            int(e)
            for e in app.customer(
                "pell_usage",
                f"select event_id from usage_events where event_id in ({','.join(map(str, facts['moved_in']))});",
            ).split()
        }
        if held != set(facts["pell_loaded"]):
            problems.append(f"pell_usage holds {sorted(held)} of the moved-in rows, expected {facts['pell_loaded']}")
        has_w = (
            app.customer(
                "larkspur_monthend", f"select count(*) from usage_events where event_id = {facts['w']};"
            ).strip()
            == "1"
        )
        if has_w != facts["w_loaded"]:
            problems.append(f"larkspur_monthend holds W: {has_w}, expected {facts['w_loaded']}")
        pending = app.finance(
            f"select count(*) from ledger_adjustments where line_id = {facts['p']} and account = 'pell' "
            "and invoice_id is null and amount < 0 and reason like 'usage moved to %';"
        ).strip()
        if pending != "1":
            problems.append("finance holds no pending credit for P")
        if (
            app.core(
                f"select count(*) from refunds where capture_id = {facts['refund_capture']};", database="payments"
            ).strip()
            != "1"
        ):
            problems.append("the refund for P is missing")
        if (
            app.core(
                f"select count(*) from usage_events where event_id in ({facts['w']}, {facts['w2']}) and tenant_id = 'larkspur';"
            ).strip()
            != "2"
        ):
            problems.append("core does not hold both W and W'")
        if (
            app.customer(
                "pell_usage",
                "select count(*) from extract_files e where status = 'ready' "
                "and not exists (select 1 from loaded_files l where l.file_id = e.file_id);",
            ).strip()
            == "0"
        ):
            problems.append("no ready correction file waits for Pell's loader")
        # What each customer copy holds now, beside it, for the statement checks.
        for db in CUSTOMER_DATABASES:
            wrong = diffs[db]["extra"] + diffs[db]["changed"]
            app.customer(
                db,
                "create schema if not exists handoff;\ndrop table if exists handoff.sealed;\n"
                "create table handoff.sealed as select event_id, line_id, true as correct from usage_events;\n"
                + (
                    f"update handoff.sealed set correct = false where event_id in ({','.join(map(str, wrong))});\n"
                    if wrong
                    else ""
                ),
            )
        # The handoff steps ran days ago; their Job records are long gone.
        app.command("delete", "job", "-l", "app=pubsync", "--ignore-not-found", "--wait=true", timeout=180)
        handoff = app.core("select now();").strip()
        app.set_rate(60)
        return problems, {
            "handoff_at": handoff,
            "finance_gaps": finance_gaps,
            "documents": documents(app),
            "diffs": {db: {k: len(v) for k, v in d.items()} for db, d in diffs.items()},
        }

    def seal_readers(self, facts, diffs, slots):
        """Month-end ahead of its slot, Vale not, Pell resumed past a gap, Quill without the direct fix."""
        app = self.app
        problems = []
        position = {db: app.subscription_origin(db) for db in PAUSED}
        facts["origins"] = position
        ahead = app.core(
            f"select {sql_literal(position['larkspur_monthend'])}::pg_lsn > "
            f"{sql_literal(slots['larkspur_monthend']['confirmed'])}::pg_lsn;"
        ).strip()
        # The origin moves only with an applied transaction that carried month-end changes; during the pause
        # only ingest wrote, so origin > slot means Larkspur inserts are applied beyond the slot's position.
        if ahead != "t":
            problems.append(
                f"larkspur_monthend origin is not ahead of its slot: {position} {slots['larkspur_monthend']}"
            )
        if (
            app.customer(
                "postgres", "select subenabled from pg_subscription where subname = 'larkspur_monthend';"
            ).strip()
            != "t"
        ):
            problems.append("larkspur_monthend subscription is not enabled")
        behind = app.core(
            f"select {sql_literal(position['vale_usage'])}::pg_lsn <= {sql_literal(slots['vale_usage']['confirmed'])}::pg_lsn;"
        ).strip()
        if behind != "t":
            problems.append(f"vale_usage origin is ahead of its slot: {position} {slots['vale_usage']}")
        first, last = facts["pell_gap"]
        lacked = [e for e in diffs["pell_usage"]["missing"] if first < e <= last]
        facts["pell_gap_lacked"] = len(lacked)
        if not lacked:
            problems.append("pell_usage lacks no row inserted while its feed was stopped")
        if facts["pell_gap_reduced"] not in diffs["pell_usage"]["changed"]:
            problems.append("pell_usage does not hold the gap-reduced row's old quantity")
        if not set(ledger(app.finance)) - set(ledger(app.billing)):
            problems.append("finance holds no line that billing deleted")
        if not slots.get("pell_usage", {}).get("active"):
            problems.append("pell_usage is not streaming")
        held = app.customer("quill_usage", f"select quantity from usage_events where event_id = {facts['q']};").strip()
        if not held or float(held) != float(facts["q_quantity"]):
            problems.append(f"quill_usage holds {held!r} for Q, expected the old quantity {facts['q_quantity']}")
        return problems

    def save_state(self):
        body = json.dumps(
            {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {"name": "metering-state"},
                "data": {"state.json": json.dumps(self.state), "corrected.json": json.dumps(sorted(self.corrected))},
            }
        )
        self.app.hidden("apply", "-f", "-", input_text=body)

    def load_state(self):
        if not self.state:
            with contextlib.suppress(subprocess.CalledProcessError, KeyError, json.JSONDecodeError):
                data = json.loads(self.app.hidden("get", "configmap", "metering-state", "-o", "json"))["data"]
                self.state = json.loads(data["state.json"])
                self.corrected.update(json.loads(data.get("corrected.json", "[]")))
        return self.state

    @mark_fault_injected
    def inject_fault(self):
        failures = []
        for attempt in range(1, MAX_BUILDS + 1):
            if attempt > 1:
                print(f"Rebuilding the world (attempt {attempt}): {failures[-1]}")
                self.app.cleanup()
                self.app.kubectl.wait_for_namespace_deletion(self.namespace)
                self.app.deploy()
                self.corrected.clear()
                self.tickets.clear()
            try:
                facts = self.build()
            except WorldDidNotTake as exc:
                failures.append([str(exc)])
                print(f"Build did not take: {exc}")
                continue
            with self.phase("seal"):
                problems, sealed = self.seal(facts)
            if not problems:
                self.state = {**facts, **sealed}
                self.save_state()
                summary = {k: v for k, v in self.state.items() if k != "documents"}
                print(f"Sealed: {json.dumps(summary)}")
                return True
            failures.append(problems)
            print(f"Seal failed: {problems}")
        raise RuntimeError(f"world did not reach the incident state in {MAX_BUILDS} builds: {failures}")

    # --------------------------------------------------------------- recovery
    @mark_fault_injected
    def recover_fault(self):
        """The full reference repair, through the shared feeds, as an operator on core could do it."""
        from sregym.conductor.problems.apps.metering.reference_repair import repair

        repair(self)
        return True
