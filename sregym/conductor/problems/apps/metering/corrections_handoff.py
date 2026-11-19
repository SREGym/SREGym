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
out from it. Every server has since been patched to 17.6; only the damage
remains.
"""

import contextlib
import json
import random
import subprocess
import time

from sregym.conductor.oracles.apps.metering.corrections_handoff import (
    MeteringOracle,
    copy_digest,
    finance_differences,
)
from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.problems.base import Problem
from sregym.service.apps.metering.application import (
    CUSTOMER_DATABASES,
    NEW_IMAGE,
    RECEIPT_DATABASE,
    STREAMED,
    TENANTS,
    MeteringApplication,
    ledger_conf,
    publications_conf,
    sql_literal,
)
from sregym.utils.decorators import mark_fault_injected

VICTIMS = ("pell_usage", "quill_usage", "larkspur_live")
SURVIVORS = ("larkspur_monthend", "ostrava_usage", "vale_usage")
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
        self.state = {}
        self.root_cause = self.build_structured_root_cause(
            component="logical decoding on the core and billing PostgreSQL servers (statefulset/core, "
            "statefulset/billing) during pubsync's corrections handoffs, while they ran PostgreSQL 17.4",
            namespace=self.namespace,
            description=(
                "PostgreSQL 17.4 logical decoding did not pass catalog invalidations to transactions already in "
                "progress (data loss bug fixed in 17.5, commit 4909b38af034). pubsync moved corrections (UPDATE and "
                "DELETE) with a make-before-break handoff: first ALTER PUBLICATION corr_<tenant> (and, a week "
                "earlier on billing, ledger_corr) SET (publish = 'update, delete'), then after every slot confirmed "
                "past it, a quiet ALTER PUBLICATION usage_<tenant> (ledger_all) SET (publish = 'insert'). Each make "
                "step committed while an ingest batch (or billing apply transaction) that had already written was "
                "in flight; every walsender then running decoded that transaction first and cached the corrections "
                "publication's pre-change definition (publishing nothing) under its older snapshot. The later break "
                "was loaded fresh, so those walsenders published inserts only, a configuration never committed, "
                "until their own next restart; readers whose walsender restarted between the steps were fine. "
                "Affected: finance's subscription finance_ledger to billing (week one, until core's restart; it "
                "missed ledger re-ratings, voids and moves, and invoices and support credit notes were issued from "
                "that ledger), and the customer subscriptions pell_usage, quill_usage and larkspur_live (from the "
                "break until each one's own restart), which kept voided and moved-out rows, stale quantities, and "
                "lacked moved-in rows; larkspur_monthend, ostrava_usage, vale_usage and billing were not affected. "
                "Gone since both servers were patched to 17.6, so only the damage remains. Not the cause: the "
                "collation version warnings (they started with the 17.6 image), the customers' databases, billing's "
                "rating, finance's closed-period rule (by design), the add-table race (pubsync locks the table), "
                "subscriber lag, or a need to reconnect after publication changes."
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

    def correct(self, tenant, kind, to=None, invoiced=None):
        event = self.pick_event(tenant, invoiced)
        value = "null"
        if kind == "reduce":
            quantity = float(self.app.core(f"select quantity from usage_events where event_id = {event};").strip())
            value = sql_literal(f"{round(quantity * self.rng.choice((0.25, 0.4, 0.5, 0.6)), 3):.3f}")
        elif kind == "move":
            value = sql_literal(to)
        reason = self.rng.choice(REASONS[kind]).format(to=TENANTS.get(to, ("",))[0])
        self.app.core(
            f"select correct_usage({event}, {sql_literal(kind)}, {value}, {sql_literal(reason)}, "
            f"{sql_literal(self.rng.choice(STAFF))});"
        )
        return event

    def corrections(self, plan, pause=(1, 4)):
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

    # ---------------------------------------------------------------- history
    def build(self):
        app = self.app
        app.pubsync(
            "core", publications_conf("r1", "initial feeds", {t: ("insert,update,delete", "") for t in STREAMED}), "r1"
        )
        app.pubsync("billing", ledger_conf("r1", "ledger feed for finance", "insert,update,delete", ""), "r1")
        app.subscribe_billing()
        app.subscribe_finance()
        app.subscribe_customers()
        app.set_rate(60)
        time.sleep(90)

        # Months of ordinary operation, compressed: corrections flow to every reader; first invoices.
        self.corrections(self.random_plan(10))
        time.sleep(30)
        app.invoice_run(list(TENANTS), "invoice-run-close-1")
        time.sleep(60)
        self.corrections(self.random_plan(8, invoiced=True))
        time.sleep(30)

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
        credits = self.issue_credits(week1)
        time.sleep(20)
        # Core restarts for more replication slots (onboarding headroom); finance's walsender on billing restarts with it.
        app.set_server("core", senders=32)
        time.sleep(30)
        self.corrections(self.random_plan(6))
        time.sleep(20)
        app.invoice_run([t for t in TENANTS if t not in first_half], "invoice-run-close-3")
        time.sleep(30)

        # Week two: the same handoff for every opted-in customer on core, during ingest's busy hour.
        app.set_rate(1500)
        time.sleep(25)
        app.pubsync(
            "core",
            publications_conf(
                "r2",
                "customer corrections: enable updates and deletes on corr_* (1/2)",
                {t: ("insert,update,delete", "update,delete") for t in STREAMED},
            ),
            "r2",
        )
        app.set_rate(150)
        target = app.core("select pg_current_wal_lsn();").strip()
        time.sleep(15)
        app.wait_caught_up(slots=list(SURVIVORS))
        for db in SURVIVORS:
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
                ("vale", "move", "pell", None),
                ("larkspur", "reduce", None, None),
                ("pell", "void", None, True),
                ("larkspur", "void", None, True),
                ("quill", "reduce", None, True),
                ("garnet", "move", "larkspur", True),
            ]
            + self.random_plan(4)
        )
        time.sleep(30)
        for db in VICTIMS:
            digest = copy_digest(app, db, CUSTOMER_DATABASES[db])
            if not set(week2) & {e for kind in digest.values() for e in kind}:
                raise WorldDidNotTake(f"{db} received every week-two correction")
        app.restart_subscription("pell_usage")
        time.sleep(20)
        self.corrections(
            [
                ("quill", "void", None, None),
                ("larkspur", "void", None, None),
                ("brisk", "move", "larkspur", None),
                ("quill", "move", "vale", None),
                ("pell", "void", None, None),
                ("larkspur", "reduce", None, True),
            ]
            + self.random_plan(4)
        )
        time.sleep(30)
        app.restart_subscription("quill_usage")
        time.sleep(8)
        app.restart_subscription("larkspur_live")
        time.sleep(20)
        self.corrections(self.random_plan(6))
        time.sleep(20)
        app.invoice_run(["pell", "quill", "garnet", "hollis"], "invoice-run-close-4")
        time.sleep(20)

        # Routine patching moves both servers to the current minor release.
        app.set_server("billing", image=NEW_IMAGE)
        app.set_server("core", image=NEW_IMAGE)
        time.sleep(45)
        return {"credits": len(credits), "target": target}

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
            ticket = f"SUP-{self.rng.randint(4100, 4999)}"
            memo = f"Disputed usage on invoice {line['invoice']}: voided usage was billed ({ticket})"
            app.finance(
                f"insert into credit_notes (account, amount, memo, issued_by) values ({sql_literal(account)}, "
                f"{line['amount']}, {sql_literal(memo)}, {sql_literal(self.rng.choice(STAFF))});"
            )
            credits.append({**line, "ticket": ticket})
        return credits

    # ------------------------------------------------------------------- seal
    def seal(self):
        """Assert the per-reader outcome the story needs; returns the facts the oracle keeps."""
        app = self.app
        app.set_rate(0)
        app.wait_inbox_empty()
        app.wait_caught_up()
        app.wait_caught_up(app.billing)
        time.sleep(5)
        problems = []
        diffs = {}
        for db, tenant in CUSTOMER_DATABASES.items():
            diffs[db] = copy_digest(app, db, tenant)
        for db in VICTIMS:
            if not any(diffs[db].values()):
                problems.append(f"{db} received every correction")
        for db in SURVIVORS:
            if any(diffs[db].values()):
                problems.append(f"{db} missed corrections: {diffs[db]}")
        if not diffs["pell_usage"]["extra"]:
            problems.append("pell_usage holds no voided or moved-out row")
        if not set(diffs["larkspur_live"]["missing"]):
            problems.append("larkspur_live lacks no moved-in row")
        finance_gaps = self.finance_gaps()
        if not finance_gaps:
            problems.append("finance received every ledger change")
        # What the month-end database has stamped so far, kept beside it for the checks.
        app.customer(
            RECEIPT_DATABASE,
            "create schema if not exists handoff;\ndrop table if exists handoff.receipts;\n"
            "create table handoff.receipts as select event_id, export_receipt from usage_events "
            "where export_receipt is not null;\n",
        )
        handoff = app.core("select now();").strip()
        app.set_rate(60)
        return problems, {
            "handoff_at": handoff,
            "finance_gaps": finance_gaps,
            "diffs": {db: {k: len(v) for k, v in d.items()} for db, d in diffs.items()},
        }

    def finance_gaps(self):
        """Ledger lines whose finance value under its own rule differs from billing's."""
        return len(finance_differences(self.app))

    def save_state(self):
        body = json.dumps(
            {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {"name": "metering-state"},
                "data": {"state.json": json.dumps(self.state)},
            }
        )
        self.app.hidden("apply", "-f", "-", input_text=body)

    def load_state(self):
        if not self.state:
            with contextlib.suppress(subprocess.CalledProcessError, KeyError, json.JSONDecodeError):
                data = json.loads(self.app.hidden("get", "configmap", "metering-state", "-o", "json"))["data"]
                self.state = json.loads(data["state.json"])
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
            try:
                facts = self.build()
            except WorldDidNotTake as exc:
                failures.append([str(exc)])
                print(f"Build did not take: {exc}")
                continue
            problems, sealed = self.seal()
            if not problems:
                self.state = {**facts, **sealed}
                self.save_state()
                print(f"Sealed: {json.dumps(sealed['diffs'])}, finance gaps {sealed['finance_gaps']}")
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
