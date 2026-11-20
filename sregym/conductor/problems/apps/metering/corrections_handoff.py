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

After that link restarted, billing released rate bands during its hourly
re-rate. Its sender kept writing the old rate format to finance. Finance
received the usage but used list price where the committed band was missing.

While four customer feeds dropped corrections, support's bulk correction
CF-31 and the patches it caused piled up: files of corrections for customers to
apply by hand, a usage row keyed in again, a refund, partial reverts (one of
which stopped Pell's feed on a duplicate key; on-call moved its slot forward
past everything sent meanwhile), a quantity fixed directly on core, nightly
correction files that Pell receives by subscription and loads automatically,
an omission on Pell's statement return recorded as a waiver in finance,
two Quill statement discrepancies when re-dates crossed the open-period filter,
and two readers paused with a backlog (Larkspur's month-end copy, which stops on
its first error, during its close; Vale during its site maintenance). Every
server has since been patched to 17.6. Finance later combined returns from
different revisions of a four-account allocation, waiving earned usage that
was present in the correct customer copies at settlement.
"""

import contextlib
import json
import random
import subprocess
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

from sregym.conductor.oracles.apps.metering.corrections_handoff import (
    MeteringOracle,
    copy_digest,
    documents,
    finance_differences,
    ledger,
    ledger_rates,
    rate_band,
)
from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.problems.apps.metering import complaints
from sregym.conductor.problems.apps.metering.allocation_close import AllocationReplay, seal_allocations
from sregym.conductor.problems.base import Problem
from sregym.service.apps.metering.application import (
    COMMITTED_DISCOUNTS,
    CUSTOMER_DATABASES,
    STREAMED,
    TENANTS,
    MeteringApplication,
    extracts_conf,
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
    "redate": ("usage timestamp corrected from the meter export",),
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


complaints.WorldDidNotTake = WorldDidNotTake


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
            component="Finance's acceptance of a never-committed allocation at the point where it reconciled four individually correct customer snapshots from different approved revisions against one eligible close allocation.",
            namespace=self.namespace,
            description="For the four-return close, each PostgreSQL 17.6 REPEATABLE READ transaction fixed its view at a different approved revision. Pell read revision 5 and returned A, Quill read revision 3 and returned B, Larkspur live read revision 1 and returned C, and Ostrava read revision 4 and returned D. Every return was correct in its own view. Together the returns assigned A, B, C, D to those customers, an allocation core never committed. Revision 6 was the last approval eligible at support's cutoff and assigned A, B, D, C. The revision-6 settlement acknowledgements show the feeds delivered that allocation before the files were signed. The later amendment belonged to the next close.\n\nFinance imported the returns one tenant at a time against the eligible allocation, matching by tenant and event ID within the close's period. It treated the returns as if their ownership agreed on one allocation. Its MERGE WHEN NOT MATCHED BY SOURCE treated the missing eligible charges as waivers. Larkspur live returned C instead of its earned D ($60,000), and Ostrava returned D instead of its earned C ($40,000). Pell and Quill returned their eligible lots. Customers resend the same signed files until acknowledged, so clearing the waivers without recording revocations lets the next import waive the charges again.\n\nFor this close, locating the fault means identifying Finance's reconciliation of locally correct returns pinned to different approved revisions as one allocation. The scope is the joint four-return allocation that core never committed and the two false waivers that followed even though both customers had received their eligible lots. An account of origin or scope must tie the component or impact it names to that incompatible combination. Naming where the waivers were written, who received them, or what was missing describes the outcome. Exact revision numbers, amounts, lot labels and SQL syntax are supporting details.\n\nSeparately, PostgreSQL 17.4 logical decoding missed catalog invalidations for already-running transactions (fixed in 17.5 by 4909b38af034). During pubsync's corrections handoff, stale publication actions caused the Pell, Quill, Larkspur live, Ostrava and billing-to-finance senders to omit updates and deletes until their own restarts. Quill's UPDATE-only open-period publication still transformed re-dates across its filter into an INSERT or DELETE. The period-partitioned copy booked the incoming usage twice and lost the outgoing usage. Billing's stale composite rate_info encoding omitted the r5 band on a separate cohort, leading finance to apply list prices to committed usage. Pell's earlier return also turned an actual missing transfer into a false waiver. The four allocation lots were created after the 17.6 patch and were independent of that earlier damage.",
        )
        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)
        self.mitigation_oracle = MeteringOracle(problem=self)

    # ------------------------------------------------------------ corrections
    def pick_events(self, plan):
        app = self.app
        picked = {}
        for invoiced in (True, False, None):
            counts = Counter(tenant for tenant, _, _, status in plan if status is invoiced)
            if not counts:
                continue
            exclude = ",".join(str(e) for e in self.corrected) or "0"
            queries = []
            for tenant, count in counts.items():
                if invoiced is None:
                    source = (
                        f"event_id from usage_events where tenant_id = {sql_literal(tenant)} "
                        f"and event_id not in ({exclude}) "
                        "and event_id > (select max(event_id) - 3000 from usage_events)"
                    )
                else:
                    source = (
                        f"line_id from ledger_lines where account = {sql_literal(tenant)} "
                        f"and invoice_id is {'not ' if invoiced else ''}null and line_id not in ({exclude}) "
                        "and not exists (select 1 from ledger_adjustments a where a.line_id = ledger_lines.line_id)"
                    )
                queries.append(
                    f"select {sql_literal(tenant)} || '|' || coalesce(array_to_string(array(select {source} "
                    f"order by random() limit {count}), ','), '');"
                )
            run = app.core if invoiced is None else app.finance
            for line in run("\n".join(queries)).splitlines():
                tenant, _, events = line.partition("|")
                picked[tenant, invoiced] = list(map(int, events.split(","))) if events else []
            for tenant, count in counts.items():
                events = picked[tenant, invoiced]
                if len(events) != count:
                    raise RuntimeError(f"not enough events to correct for {tenant} (invoiced={invoiced})")
                self.corrected.update(events)
        return [picked[tenant, invoiced].pop() for tenant, _, _, invoiced in plan]

    def correction_value(self, event, kind, to=None, *, quantity=None):
        if kind == "reduce":
            if quantity is None:
                quantity = self.app.core(f"select quantity from usage_events where event_id = {event};").strip()
            return f"{round(float(quantity) * self.rng.choice((0.25, 0.4, 0.5, 0.6)), 3):.3f}"
        return to if kind in ("move", "redate") else None

    def reason(self, kind, to=None):
        return self.rng.choice(REASONS[kind]).format(to=TENANTS.get(to, ("",))[0])

    def correction_sql(self, event, kind, to, *, quantity=None):
        value = self.correction_value(event, kind, to, quantity=quantity)
        return (
            f"select correct_usage({event}, {sql_literal(kind)}, {'null' if value is None else sql_literal(value)}, "
            f"{sql_literal(self.reason(kind, to))}, {sql_literal(self.rng.choice(STAFF))});"
        )

    def correct(self, tenant, kind, to=None, invoiced=None, event=None):
        if event is None:
            return self.corrections([(tenant, kind, to, invoiced)])[0]
        self.corrected.add(event)
        self.app.core(self.correction_sql(event, kind, to))
        return event

    def corrections(self, plan, pause=None):
        if pause is not None:
            events = []
            for spec in plan:
                events.append(self.correct(*spec))
                time.sleep(self.rng.uniform(*pause))
            return events
        if not plan:
            return []
        events = self.pick_events(plan)
        quantities = self.app.core(
            "select event_id || '|' || quantity from usage_events "
            f"where event_id in ({','.join(map(str, events))});"
        )
        by_event = dict(line.split("|") for line in quantities.splitlines())
        # One session, with each ordinary correction still committed independently.
        self.app.core(
            "\n".join(
                self.correction_sql(event, kind, to, quantity=by_event[str(event)])
                for event, (_, kind, to, _) in zip(events, plan, strict=True)
            )
        )
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
        create = (
            f"insert into tickets (ticket_id, account, subject, status) values ({sql_literal(ticket)}, "
            f"{'null' if account is None else sql_literal(account)}, {sql_literal(subject)}, {sql_literal(status)});"
        )
        self.app.core(
            "\n".join([create, *(self.note_sql(ticket, author, body) for author, body in notes)]), database="support"
        )
        return ticket

    @staticmethod
    def note_sql(ticket, author, body):
        return (
            f"insert into ticket_notes (ticket_id, author, body) values ({sql_literal(ticket)}, "
            f"{sql_literal(author)}, {sql_literal(body)});"
        )

    def note(self, ticket, author, body):
        self.app.core(self.note_sql(ticket, author, body), database="support")

    @staticmethod
    def customer_name(tenant):
        return f"{TENANTS[tenant][0]} (customer)"

    def staff(self):
        return self.rng.choice(STAFF)

    # ---------------------------------------------------------------- picking
    def events_of(self, tenant, outside_period=None):
        where = f"tenant_id = {sql_literal(tenant)}"
        if outside_period:
            start, end = map(sql_literal, outside_period)
            where += f" and (occurred_at < {start} or occurred_at >= {end})"
        return {int(e) for e in self.app.core(f"select event_id from usage_events where {where};").split()}

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

    def statement_dispute(self, period):
        """Pell and support agree to leave an existing charge off the upcoming return."""
        start, end = map(sql_literal, period)
        used = ",".join(map(str, sorted(self.corrected)))
        pair = self.app.rows(
            self.app.core,
            f"""
            with period_usage as materialized (
                select event_id, tenant_id, occurred_at, metric, quantity from usage_events
                where occurred_at >= {start} and occurred_at < {end}
                  and tenant_id in ('pell', 'arden') and event_id not in ({used})
            )
            select json_build_object('genuine', g.event_id, 'missing', m.event_id, 'metric', g.metric,
                                     'quantity', g.quantity, 'at', g.occurred_at)
            from period_usage g
            cross join lateral (
                select event_id from period_usage
                where tenant_id = 'arden' and metric = g.metric and quantity <> g.quantity
                  and quantity between g.quantity * 0.99 and g.quantity * 1.01
                order by abs(quantity - g.quantity), event_id limit 1
            ) m
            where g.tenant_id = 'pell' and g.quantity >= 20
            order by g.occurred_at, g.event_id limit 1;
            """,
        )
        if not pair:
            raise WorldDidNotTake("no comparable charges in Pell's statement period")
        pair = pair[0]
        self.corrected.update((pair["genuine"], pair["missing"]))
        at = pair["at"][:19].replace("T", " ")
        self.ticket(
            "pell",
            "Disputed usage on upcoming statement",
            [
                (
                    self.customer_name("pell"),
                    f"We dispute the {pair['metric']} charge of {pair['quantity']} units "
                    f"from {at} UTC. That usage was part of our acceptance tests.",
                ),
                (self.staff(), "We agreed to waive that charge. Please leave it off your statement return."),
            ],
            status="closed",
        )
        return {"genuine": pair["genuine"], "missing": pair["missing"]}

    # ---------------------------------------------------------------- history
    @contextlib.contextmanager
    def phase(self, name):
        start = time.monotonic()
        try:
            yield
        finally:
            print(f"phase.{name}.duration_s={time.monotonic() - start:.0f}", flush=True)

    def build(self):
        app = self.app
        with self.phase("ordinary"):
            self.ordinary()
        with self.phase("week_one"):
            credits, period = self.week_one()
            statement_return = self.statement_dispute(period)
        with self.phase("week_two"):
            target = self.week_two()
        with self.phase("support_story"), ThreadPoolExecutor(max_workers=2) as pool:
            # Flush the bulk replay while support handles the close, ahead of the publisher rollout.
            checkpoints = [pool.submit(run, "checkpoint;") for run in (app.core, app.billing)]
            facts = self.support_story(period, statement_return)
            self.close_invoices(["quill", "garnet", "hollis"])
            for checkpoint in checkpoints:
                checkpoint.result()
        with self.phase("patch"):
            # The paused customer slots must also retain their positions through the rollout.
            app.core("checkpoint;")
            app.patch_publishers()
            patched_at = self.app.billing("select clock_timestamp();").strip()
            self.seed_usage(5)
            self.wait_for_finance()
        with self.phase("statement_return"):
            self.app.replay_job("statement-return-import.sh", "finance")
        with self.phase("allocation_closes"):
            facts["allocation_returns"] = AllocationReplay(self).replay()
            # Pell's client retries its first return after the last close import, so it is still queued.
            statement_return["retry"] = self.app.deliver_statement_return("pell", "pell_usage", statement_return["ref"])
        with self.phase("complaints"):
            facts["complaints"] = complaints.ComplaintReplay(self).run(self.planted)
        return {"credits": len(credits), "target": target, "rates_patched_at": patched_at, **facts}

    def seed_usage(self, batches, *, starting_at=None):
        # Commit in chunks so billing and finance can apply while core writes the next chunk.
        self.app.core(
            "\n".join(self.app.usage_batch_sql(min(100, batches - start), 60, starting_at=starting_at)
                for start in range(0, batches, 100))
        )

    def wait_for_finance(self):
        # The ledger on billing is written by the core subscription before finance can copy it.
        self.app.wait_caught_up(slots=["billing_usage"])
        self.app.wait_caught_up(self.app.billing)

    def close_invoices(self, accounts):
        self.app.replay_job("invoice-run.sh", "finance", *accounts)
        self.app.capture_invoices()

    def ordinary(self):
        app = self.app
        app.truncate_server_logs()
        with ThreadPoolExecutor(max_workers=2) as pool:
            billing = pool.submit(
                app.replay_pubsync, "billing", ledger_conf("r1", "ledger feed for finance", "insert,update,delete", "")
            )
            app.replay_pubsync(
                "core",
                publications_conf(
                    "r1", "initial feeds", {t: ("insert,update,delete", "") for t in STREAMED},
                    quill_period=app.quill_period,
                ),
            )
            billing.result()
        # Finance shares core's PostgreSQL instance. Creating both reciprocal
        # subscriptions together makes each slot wait for the other's transaction.
        app.subscribe_billing(copy_data=False)
        app.subscribe_finance(copy_data=False)
        app.publish_allocation_receipts()
        app.subscribe_customers(copy_data=False)
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
        # Months of ordinary operation, compressed: corrections flow to every reader; first invoices.
        prior_period = f"DATE {sql_literal(app.quill_period)} - interval '1 month' + interval '1 hour'"
        self.seed_usage(105, starting_at=prior_period)
        self.corrections(self.random_plan(10))
        self.wait_for_finance()
        self.close_invoices(TENANTS)
        self.seed_usage(80)
        self.corrections(self.random_plan(8, invoiced=True))
        app.wait_caught_up()

    def week_one(self):
        app = self.app
        # Week one: billing's ledger corrections move to ledger_corr during billing's busy hour.
        self.seed_usage(875)
        self.wait_for_finance()
        target = app.publication_handoff(
            "billing",
            ledger_conf(
                "r2",
                "finance corrections: enable updates and deletes on ledger_corr (1/2)",
                "insert,update,delete",
                "update,delete",
            ),
        )
        app.wait_caught_up(app.billing, target=target)
        self.corrections(self.random_plan(4))
        self.wait_for_finance()
        app.replay_pubsync(
            "billing",
            ledger_conf(
                "r3",
                "finance corrections: withdraw updates and deletes from ledger_all (2/2)",
                "insert",
                "update,delete",
            ),
        )
        self.seed_usage(20)
        self.wait_for_finance()
        week1 = self.corrections(
            [(t, "void", None, True) for t in ("pell", "arden", "cinder", "garnet")]
            + [(t, "void", None, False) for t in ("pell", "larkspur", "arden", "quill", "hollis")]
            + [(t, "reduce", None, True) for t in ("larkspur", "brisk", "ostrava")]
            + [(t, "reduce", None, False) for t in ("garnet", "vale", "dorian", "pell")]
            + [("cinder", "move", "ember", True), ("arden", "move", "pell", False), ("ionic", "move", "fallow", False)]
            + self.random_plan(5)
        )
        self.wait_for_finance()
        missed = {line for line, _account in finance_differences(app)} & set(week1)
        if not missed:
            raise WorldDidNotTake("finance received every week-one ledger change")
        first_half = ["pell", "arden", "cinder", "garnet", "brisk", "larkspur", "ionic"]
        self.close_invoices(first_half)
        credits = self.issue_credits(week1)
        # Finance's DBA maintenance: its subscription to billing is disabled and enabled again.
        app.bounce_finance()
        target = app.release_rate_bands()
        app.wait_caught_up(app.billing, target=target)
        # This closed statement covers only this earlier set of readings.
        # Live ingest and later history must fall outside its return window.
        period = tuple(app.core(
            "select start_at || '|' || (start_at + interval '20 seconds') "
            "from (select now() - interval '1 day' as start_at) t;"
        ).strip().split("|"))
        self.seed_usage(65, starting_at=f"{sql_literal(period[0])}::timestamptz")
        self.corrections(self.random_plan(6))
        self.wait_for_finance()
        self.close_invoices(t for t in TENANTS if t not in first_half)
        self.rate_ticket()
        return credits, period

    def rate_ticket(self):
        app = self.app
        released = app.billing("select released_at from ops.billing_releases where revision = 'r5 rate bands';").strip()
        row = app.finance(
            "select l.invoice_id || '|' || l.line_id from ledger_lines l join invoices v using (invoice_id) "
            "where l.account = 'quill' and l.metric = 'api_calls' and v.status = 'sent' "
            f"and l.rated_at > {sql_literal(released)}::timestamptz order by l.line_id limit 1;"
        ).strip()
        if not row:
            raise WorldDidNotTake("Quill has no sent invoice for the rate-band release")
        invoice, line = row.split("|")
        self.ticket(
            "quill",
            f"Committed usage charged at list price on invoice {invoice}",
            [
                (
                    self.customer_name("quill"),
                    f"Usage line {line} was billed at list price. Our committed-use rate applies.",
                ),
                (self.staff(), "I checked the invoice against finance's ledger. The amounts match."),
            ],
        )

    def week_two(self):
        app = self.app
        # Week two: the same handoff for every opted-in customer on core, during ingest's busy hour.
        self.seed_usage(675)
        app.wait_caught_up()
        # Ostrava's link drops just before the first step and is back right after it, before its slot has
        # confirmed past the ingest batch in flight at the step.
        app.customer("ostrava_usage", "alter subscription ostrava_usage disable;")
        app.wait_slot("ostrava_usage", False)
        target = app.publication_handoff(
            "core",
            publications_conf(
                "r2",
                "customer corrections: enable updates and deletes on corr_* (1/2)",
                {t: ("insert,update,delete", "update,delete") for t in STREAMED},
                quill_period=app.quill_period,
            ),
        )
        app.customer("ostrava_usage", "alter subscription ostrava_usage enable;")
        app.wait_slot("ostrava_usage", True)
        # Month-end's and Vale's links drop and reconnect after their slots confirmed past that batch.
        survivors = ("larkspur_monthend", "vale_usage")
        app.wait_caught_up(slots=survivors, target=target)
        with ThreadPoolExecutor(max_workers=len(survivors)) as pool:
            list(pool.map(app.restart_subscription, survivors))
        app.wait_caught_up(slots=["ostrava_usage"], target=target)
        self.corrections(self.random_plan(5))
        app.wait_caught_up()
        app.replay_pubsync(
            "core",
            publications_conf(
                "r3", "customer corrections: withdraw updates and deletes from usage_* (2/2)", DECLARED_CORE,
                quill_period=app.quill_period,
            ),
        )
        self.seed_usage(30)
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
        # Ordinary and contract moves around the three customers who will complain after the patch.
        self.planted = complaints.ComplaintReplay(self).plant()
        app.wait_caught_up()
        for db in VICTIMS:
            digest = copy_digest(app, db, CUSTOMER_DATABASES[db])
            if not set(week2) & {e for kind in digest.values() for e in kind}:
                raise WorldDidNotTake(f"{db} received every week-two correction")
        return target

    def support_story(self, return_period, statement_return):
        """Support's bulk correction CF-31 and the patches it caused, while four feeds still drop corrections."""
        app = self.app
        lark, pell = self.customer_name("larkspur"), self.customer_name("pell")

        # Every customer closes its statement once.
        with ThreadPoolExecutor(max_workers=len(CUSTOMER_DATABASES)) as pool:
            list(pool.map(app.close_statement, CUSTOMER_DATABASES))

        quill_period = self.quill_period_corrections()

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
        app.pause_reader("larkspur_monthend", applied_ahead=True)
        app.set_rate(60)
        self.note(month_end, f"on-call: {self.staff()}", "Paused the month-end feed for Larkspur.")

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
            self.choose(self.events_of("arden", outside_period=return_period), "move into Pell"),
            self.choose(self.events_of("cinder", outside_period=return_period), "move into Pell"),
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
                (statement_return["missing"], "move", "pell"),
                (quill, "reduce", None),
            ],
        )
        review = self.ticket(
            None,
            "Account review CF-31: usage corrections",
            [(self.staff(), "Applied the CF-31 corrections from the account review as one batch.")],
            status="closed",
        )

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
        close = app.close_statement("pell_usage")
        return_ref = f"PELL-{close}"
        start, end = map(sql_literal, return_period)
        app.customer(
            "pell_usage",
            f"select save_statement_return({sql_literal(return_ref)}, {start}, {end}, "
            f"array[{statement_return['genuine']}]::bigint[]);",
        )
        statement_return.update(
            ref=return_ref, review=review, first=app.deliver_statement_return("pell", "pell_usage", return_ref)
        )
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
            f"where tenant_id = 'quill' and billing_period < DATE {sql_literal(app.quill_period)} "
            "and metric = 'api_calls' and quantity >= 2 "
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
        extract_accounts = {"pell": pell_ticket, "larkspur": lark_ticket}
        app.setup_extracts(extract_accounts)
        app.subscribe_files("pell", "pell_usage")
        for ticket in (pell_ticket, lark_ticket):
            self.note(
                ticket,
                self.staff(),
                "Until the feed is fixed we will send you a file of corrections for your account every night.",
            )
        app.replay_job("correction-extracts.sh", "usage", config=extracts_conf(extract_accounts))
        app.load_extract_files("pell", "pell_usage")
        self.note(pell_ticket, pell, "Thanks, we load your nightly file automatically.")

        # An ordinary correction later reduces one of the rows moved into Pell.
        reduced = self.rng.choice(moved_in)
        self.correct("pell", "reduce", event=reduced)

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
        app.wait_caught_up(slots=["ostrava_usage"])
        app.close_statement("ostrava_usage")
        self.note(
            month_end,
            lark,
            "Close is confirmed on our side and our DBA has re-enabled the month-end subscription. "
            "Please resume the month-end feed.",
        )
        app.customer("larkspur_monthend", "alter subscription larkspur_monthend enable;")

        # Pell reconnects before its next correction. The other links recover after the skip.
        app.wait_caught_up(slots=["pell_usage"])
        app.restart_subscription("pell_usage")
        gap = self.pell_gap(review, y)
        reconnecting = ("quill_usage", "larkspur_live")
        app.wait_caught_up(slots=reconnecting)
        with ThreadPoolExecutor(max_workers=len(reconnecting)) as pool:
            list(pool.map(app.restart_subscription, reconnecting))
        self.corrections([("pell", "reduce", None, None)] + self.random_plan(5))
        app.set_rate(0)
        app.wait_inbox_empty()
        app.wait_caught_up(skip=PAUSED)
        self.wait_for_finance()
        app.replay_job("correction-extracts.sh", "usage", config=extracts_conf(extract_accounts))
        app.wait_caught_up(slots=["files_pell"])
        # The customers that are online close their statements as every day.
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(app.close_statement, VICTIMS))
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
            "statement_return": statement_return,
            "quill_period": quill_period,
            **gap,
        }

    def quill_period_corrections(self):
        app = self.app
        open_period = sql_literal(app.quill_period)
        booked = ",".join(map(str, sorted(self.issued("quill_usage"))))
        used = ",".join(map(str, sorted(self.corrected)))
        rows = app.rows(
            app.core,
            f"""
            select distinct on (billing_period) json_build_object(
                'event', event_id, 'record', record_id, 'before', occurred_at,
                'from_period', billing_period,
                'after', occurred_at + case when billing_period = DATE {open_period}
                                          then -interval '1 month' else interval '1 month' end,
                'to_period', case when billing_period = DATE {open_period}
                                  then DATE {open_period} - interval '1 month' else DATE {open_period} end
            )
            from usage_events
            where tenant_id = 'quill' and event_id in ({booked}) and event_id not in ({used})
              and billing_period in (DATE {open_period}, DATE {open_period} - interval '1 month')
            order by billing_period, event_id;
            """,
        )
        if len(rows) != 2:
            raise WorldDidNotTake("Quill did not book usable rows in both billing periods")
        entering, leaving = rows
        for item in rows:
            item["to_period"] = item["to_period"][:10]
        self.correct_batch("Quill statement dates", [(r["event"], "redate", r["after"]) for r in rows])
        app.wait_caught_up(slots=["quill_usage"])
        app.close_statement("quill_usage")
        for case, subject, symptom in (
            (entering, "Usage appears on two statements", "It now appears on both statements."),
            (leaving, "Reversed usage was not re-issued", "The old charge was reversed, but the new statement is missing it."),
        ):
            self.ticket(
                "quill",
                subject,
                [
                    (self.staff(), f"We corrected the date for record {case['record']} "
                     f"from {case['before'][:10]} to {case['after'][:10]}."),
                    (self.customer_name("quill"), symptom),
                ],
            )
        return {"entering": entering, "leaving": leaving}

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
        app.pause_reader("pell_usage")
        app.set_rate(0)
        app.wait_inbox_empty()
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
        self.seed_usage(85)
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
        skipped_to = app.core(
            "select end_lsn from pg_replication_slot_advance('pell_usage', pg_current_wal_lsn());"
        ).strip()
        app.core("alter role feed_pell_usage login;")
        last = int(app.core("select max(event_id) from usage_events;").strip())
        self.note(failing, f"on-call: {self.staff()}", "Skipped the failing change on our side; feed resumed.")
        app.wait_slot("pell_usage", True)
        app.set_rate(60)
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
        problems = []
        with ThreadPoolExecutor(max_workers=len(CUSTOMER_DATABASES)) as pool:
            readings = {db: pool.submit(copy_digest, app, db, tenant) for db, tenant in CUSTOMER_DATABASES.items()}
            diffs = {db: reading.result() for db, reading in readings.items()}
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
        rate_problems, rate_counts = self.seal_rates(facts["rates_patched_at"])
        problems += rate_problems
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
        facts["paused_slots"] = {db: slots[db]["confirmed"] for db in PAUSED if db in slots}
        problems += self.seal_readers(facts, diffs, slots)
        problems += self.seal_statement_return(facts["statement_return"], diffs["pell_usage"])
        problems += self.seal_quill_period(facts["quill_period"])
        allocation_problems, allocation_values = seal_allocations(app, facts["allocation_returns"], facts["statement_return"])
        problems += allocation_problems
        quill = facts["quill_period"]
        pitfalls = {
            facts[k] for k in ("x", "z", "w", "w2", "v1", "p", "y", "q", "pell_gap_reduced")
        } | set(facts["pell_voids"]) | set(facts["moved_in"]) | {
            facts["statement_return"]["genuine"], facts["statement_return"]["missing"],
            quill["entering"]["event"], quill["leaving"]["event"],
        } | {row["line"] for key in ("earned", "genuine") for row in facts["allocation_returns"].get(key, [])}
        problems += complaints.ComplaintReplay(self).seal(facts["complaints"], pitfalls)
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
        def save_reader_snapshot(db):
            wrong = diffs[db]["extra"] + diffs[db]["changed"]
            stray = ""
            if db == "quill_usage":
                entering = facts["quill_period"]["entering"]
                wrong = [event for event in wrong if event != entering["event"]]
                stray = (
                    f"update handoff.sealed set correct = false where event_id = {entering['event']} "
                    f"and billing_period = DATE {sql_literal(entering['from_period'])};\n"
                )
            app.customer(
                db,
                "create schema if not exists handoff;\ndrop table if exists handoff.sealed;\n"
                "create table handoff.sealed as select event_id, billing_period, line_id, true as correct from usage_events;\n"
                + stray
                + (
                    f"update handoff.sealed set correct = false where event_id in ({','.join(map(str, wrong))});\n"
                    if wrong
                    else ""
                ),
            )
        with ThreadPoolExecutor(max_workers=len(CUSTOMER_DATABASES)) as pool:
            list(pool.map(save_reader_snapshot, CUSTOMER_DATABASES))
        handoff = app.core("select now();").strip()
        app.set_rate(60)
        return problems, {
            "handoff_at": handoff,
            "finance_gaps": finance_gaps,
            "rates": rate_counts,
            "allocation_seal": allocation_values,
            "documents": documents(app),
            "diffs": {db: {k: len(v) for k, v in d.items()} for db, d in diffs.items()},
        }

    def seal_rates(self, patched_at):
        app = self.app
        finance = ledger_rates(app.finance)
        rows = app.billing(
            "select line_id || '|' || rate::text || '|' || case "
            f"when rated_at >= {sql_literal(patched_at)}::timestamptz then 'patched' "
            "when rated_at >= (select released_at from ops.billing_releases where revision = 'r5 rate bands') "
            "then 'release' else 'legacy' end from ledger_lines;"
        )
        problems = []
        wrong = []
        window = set()
        counts = {"window": 0, "patched": 0, "legacy": 0, "sent_at_list": {}, "legacy_sent": {}}
        legacy = set()
        for line, rate, period in (r.split("|") for r in rows.split()):
            line = int(line)
            received = finance.get(line)
            band = rate_band(rate)
            if period == "release" and band and received and not rate_band(received):
                window.add(line)
                counts["window"] += 1
            elif period == "patched" and band and received == rate:
                counts["patched"] += 1
            elif period == "legacy" and not band and received and not rate_band(received):
                legacy.add(line)
                counts["legacy"] += 1
            else:
                wrong.append(line)
        if wrong:
            problems.append(f"unexpected rates on {len(wrong)} lines, including {sorted(wrong)[:10]}")
        for period in ("window", "patched", "legacy"):
            if not counts[period]:
                problems.append(f"no {period} rates")
        invoices = app.finance(
            "select l.line_id || '|' || l.account from ledger_lines l join invoices v using (invoice_id) "
            "join invoice_items i on i.invoice_id = v.invoice_id and i.line_id = l.line_id and i.kind = 'usage' "
            "join accounts a on a.account = v.account where a.committed_discount > 0 and v.status = 'sent' "
            "and i.amount = l.amount and split_part(i.rate, ',', 2) in ('', ')') "
            "and l.metric in ('api_calls', 'tokens_k');"
        )
        for line, account in (r.split("|") for r in invoices.split()):
            for key, lines in (("sent_at_list", window), ("legacy_sent", legacy)):
                if int(line) in lines:
                    counts[key][account] = counts[key].get(account, 0) + 1
        for account in COMMITTED_DISCOUNTS:
            if account not in counts["sent_at_list"] or account not in counts["legacy_sent"]:
                problems.append(f"{account} has no sent invoice with both the expected rate cases")
        return problems, counts

    def seal_quill_period(self, cases):
        app = self.app
        entering, leaving = cases["entering"], cases["leaving"]
        events = f"{entering['event']}, {leaving['event']}"
        truth = app.core(
            f"select event_id || '|' || billing_period from usage_events where event_id in ({events}) order by event_id;"
        ).split()
        if set(truth) != {f"{case['event']}|{case['to_period']}" for case in cases.values()}:
            return [f"core did not re-date both Quill events: {truth}"]
        held = app.rows(
            lambda q: app.customer("quill_usage", q),
            "select json_build_object('event', event_id, 'period', billing_period, 'line', line_id) "
            f"from usage_events where event_id in ({events}) order by billing_period;",
        )
        if [(r["event"], r["period"]) for r in held] != [
            (entering["event"], entering["from_period"]), (entering["event"], entering["to_period"])
        ]:
            return [f"Quill did not duplicate the incoming row and lose the outgoing row: {held}"]
        booked = app.rows(
            lambda q: app.customer("quill_usage", q),
            "select json_build_object('event', event_id, 'period', billing_period, 'line', line_id, 'kind', kind) "
            f"from statement_lines where event_id in ({events});",
        )
        old = [r for r in booked if r["event"] == leaving["event"]]
        active = [r for r in booked if r["event"] == entering["event"]]
        if (
            len(old) != 2 or {r["kind"] for r in old} != {"line", "reversal"}
            or {r["period"] for r in old} != {leaving["from_period"]}
            or len({r["line"] for r in old}) != 1
            or len(active) != 2 or any(r["kind"] != "line" for r in active)
            or {r["line"] for r in active} != {r["line"] for r in held}
        ):
            return [f"Quill's statement did not book both copies and reverse only the outgoing row: {booked}"]
        entering.update(stray_line=held[0]["line"], correct_line=held[1]["line"])
        leaving["reversed_line"] = old[0]["line"]
        return []

    def seal_statement_return(self, returned, pell):
        app = self.app
        genuine, missing = returned["genuine"], returned["missing"]
        ref = sql_literal(returned["ref"])
        problems = []
        if missing not in pell["missing"] or genuine in pell["missing"]:
            problems.append("Pell did not keep the disputed charge and miss the moved charge")
        booked = app.customer(
            "pell_usage", f"select distinct event_id from statement_lines where event_id in ({genuine}, {missing});"
        ).strip()
        if booked != str(genuine):
            problems.append("Pell did not book only the disputed charge before its return")
        waivers = app.rows(
            app.finance,
            "select json_build_object('line', line_id, 'source', waived_by_return, 'reason', waiver_reason, "
            "'account', account, 'open', invoice_id is null) from ledger_lines "
            f"where waived_by_return = {ref} order by line_id;",
        )
        expected = [
            {"line": event, "source": returned["ref"], "reason": "omitted from statement return", "account": "pell", "open": True}
            for event in sorted((genuine, missing))
        ]
        if waivers != expected:
            problems.append(f"finance did not record the two identical open waivers: {waivers}")
        duplicate = app.core(
            "select sent.account = 'pell' and sent.return_ref = " + ref + " and sent.imported_at is not null "
            "and retry.imported_at is null and (sent.account, sent.return_ref, sent.period_start, sent.period_end, "
            "sent.accepted_rows) = (retry.account, retry.return_ref, retry.period_start, retry.period_end, "
            "retry.accepted_rows) and jsonb_array_length(sent.accepted_rows) > 0 "
            "and not exists (select 1 from jsonb_to_recordset(sent.accepted_rows) as a(event_id bigint) "
            f"where a.event_id in ({genuine}, {missing})) "
            "from statement_return_inbox sent cross join statement_return_inbox retry "
            f"where sent.delivery_id = {returned['first']} and retry.delivery_id = {returned['retry']};",
            database="support",
        ).strip()
        if duplicate != "t":
            problems.append("the first statement return was not imported with an identical retry still queued")
        if not problems:
            print("Statement return sealed: disputed charge booked at Pell, moved charge missing, both waived, retry queued.")
        return problems

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
                with self.phase("history"):
                    facts = self.build()
                    with self.phase("seal"):
                        problems, sealed = self.seal(facts)
            except WorldDidNotTake as exc:
                failures.append([str(exc)])
                print(f"Build did not take: {exc}")
                continue
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
