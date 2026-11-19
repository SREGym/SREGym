"""End-state grading for the usage corrections handoff.

After the agent finishes, the company's routine work runs as it would tonight:
pubsync onboards a new customer from the declared configuration, support makes
fresh corrections of every kind, traffic settles, and finance's invoice run
closes every account. Then every reader is compared with the truth on core:
each customer copy, billing's ledger, finance's ledger under its own
closed-period rule, and what each customer was charged in total.
"""

import contextlib
import json
import secrets
import subprocess
import time
from collections import defaultdict
from decimal import Decimal

from sregym.conductor.oracles.base import Oracle
from sregym.service.apps.metering.application import (
    CUSTOMER_DATABASES,
    PRICES,
    RECEIPT_DATABASE,
    SUBSCRIBERS,
    TENANTS,
    sql_literal,
)

COLUMNS = "record_id, tenant_id, metric, quantity::text, occurred_at::text, batch_id"
NEW_CUSTOMER = ("wren", "Wren Studios", "wren_usage")
CENT = Decimal("0.01")


def digest_rows(text):
    return dict(line.split("|", 1) for line in text.split())


def copy_digest(app, database, tenant):
    """Rows a customer copy has that core does not, lacks, or holds with other values."""
    digest = f"select event_id || '|' || md5(concat_ws('|', {COLUMNS})) from usage_events"
    truth = digest_rows(app.core(f"{digest} where tenant_id = {sql_literal(tenant)};"))
    copy = digest_rows(app.customer(database, f"{digest};"))
    return {
        "extra": sorted(int(e) for e in copy.keys() - truth.keys()),
        "missing": sorted(int(e) for e in truth.keys() - copy.keys()),
        "changed": sorted(int(e) for e in truth.keys() & copy.keys() if truth[e] != copy[e]),
    }


def truth_lines(app):
    """What billing should hold for every usage event on core: line -> (account, amount)."""
    price = "case metric " + " ".join(f"when '{m}' then {p}" for m, p in PRICES.items()) + " end"
    rows = app.core(f"select event_id || '|' || tenant_id || '|' || round(quantity * {price}, 4) from usage_events;")
    return {int(e): (a, Decimal(x)) for e, a, x in (line.split("|") for line in rows.split())}


def ledger(run, table="ledger_lines"):
    rows = run(f"select line_id || '|' || account || '|' || amount from {table};")
    return {int(e): (a, Decimal(x)) for e, a, x in (line.split("|") for line in rows.split())}


def finance_effective(app):
    """(line, account) -> what finance will bill for it under its own rule: the line plus its adjustments."""
    effective = defaultdict(Decimal)
    for line, (account, amount) in ledger(app.finance).items():
        effective[(line, account)] += amount
    rows = app.finance(
        "select line_id || '|' || account || '|' || amount from ledger_adjustments where line_id is not null;"
    )
    for e, a, x in (line.split("|") for line in rows.split()):
        effective[(int(e), a)] += Decimal(x)
    return effective


def differences(expected, actual):
    keys = set(expected) | set(actual)
    return sorted(k for k in keys if expected.get(k, Decimal(0)) != actual.get(k, Decimal(0)))


def finance_differences(app):
    target = {(line, account): amount for line, (account, amount) in ledger(app.billing).items()}
    return differences(target, finance_effective(app))


class MeteringOracle(Oracle):
    evaluation_timeout_seconds = 2400
    FAILURE_CLASSES = {
        "customer_copy_wrong": "agent",
        "receipts_lost": "agent",
        "subscription_not_streaming": "agent",
        "billing_ledger_wrong": "agent",
        "finance_ledger_wrong": "agent",
        "usage_invoiced_twice": "agent",
        "notices_without_change": "agent",
        "charges_wrong": "agent",
        "ingest_wrong": "agent",
        "ingest_unavailable": "agent",
        "routine_jobs_failed": "agent",
    }

    @property
    def app(self):
        return self.problem.app

    # ------------------------------------------------------------- routine
    def onboard_new_customer(self):
        app = self.app
        tenant, name, database = NEW_CUSTOMER
        if app.core(f"select count(*) from tenants where tenant_id = '{tenant}';").strip() == "0":
            password = app.passwords.setdefault(f"feed_{database}", secrets.token_hex(16))
            app.core(
                f"insert into tenants values ('{tenant}', {sql_literal(name)}, 'enterprise', true);\n"
                f"create role feed_{database} login replication password {sql_literal(password)};\n"
                f"grant select on usage_events to feed_{database};\n"
            )
            app.finance(
                f"insert into accounts values ('{tenant}', {sql_literal(name)}, 26, 'billing@{tenant}.example') "
                "on conflict do nothing;"
            )
        conf = json.loads(app.command("get", "configmap", "pubsync-core", "-o", "json"))["data"]["publications.conf"]
        if f"usage_{tenant}" not in conf:
            lines = [line for line in conf.splitlines() if not line.startswith("# revision:")]
            conf = (
                "\n".join(
                    [
                        f"# revision: r4 onboard {name}",
                        *lines,
                        f"usage_{tenant} | usage_events | tenant_id = '{tenant}' | insert",
                        f"corr_{tenant} | usage_events | tenant_id = '{tenant}' | update,delete",
                    ]
                )
                + "\n"
            )
        app.pubsync("core", conf, f"r4-{int(time.time())}")
        if database not in app.customer("postgres", "select datname from pg_database;").split():
            app.psql(f"{SUBSCRIBERS}-0", "postgres", f"create database {database};\n", hidden=True)
            app.customer(
                database,
                "create table usage_events (event_id bigint primary key, record_id text not null, "
                "tenant_id text not null, metric text not null, quantity numeric(12,3) not null, "
                "occurred_at timestamptz not null, batch_id bigint not null);\n",
            )
            conninfo = (
                f"host=core.{app.namespace}.svc.cluster.local port=5432 dbname=usage "
                f"user=feed_{database} password={app.passwords[f'feed_{database}']}"
            )
            app.customer(
                database,
                f"create subscription {database} connection {sql_literal(conninfo)} "
                f"publication usage_{tenant}, corr_{tenant};\n",
            )

    def fresh_corrections(self):
        problem = self.problem
        plan = [
            ("pell", "void", None, False),
            ("quill", "reduce", None, False),
            ("larkspur", "move", "ostrava", False),
            ("arden", "move", "larkspur", False),
            ("vale", "move", "wren", False),
            ("larkspur", "reduce", None, False),
            ("garnet", "void", None, True),
        ]
        return problem.corrections(plan, pause=(0.5, 1.5))

    def wait_ledgers_delivered(self, timeout=900):
        """Billing has every usage change from core, and finance has every billing line, before invoicing."""
        app = self.app
        deadline = time.monotonic() + timeout
        try:
            app.wait_caught_up(slots=["billing_usage"], timeout=timeout)
        except TimeoutError as exc:
            return [self.fail("billing_ledger_wrong", error=str(exc)[:500])]
        while True:
            missing = set(ledger(app.billing)) - set(ledger(app.finance))
            if not missing:
                return []
            if time.monotonic() > deadline:
                return [self.fail("finance_ledger_wrong", undelivered=sorted(missing)[:20], count=len(missing))]
            time.sleep(5)

    # -------------------------------------------------------------- checks
    def subscription_errors(self):
        rows = self.app.customer(
            "postgres",
            "select subname || '|' || (apply_error_count + sync_error_count) from pg_stat_subscription_stats;",
        )
        return {name: int(n) for name, n in (line.split("|") for line in rows.split())}

    def check_readers(self, errors_before):
        app = self.app
        failures = []
        databases = {**CUSTOMER_DATABASES, NEW_CUSTOMER[2]: NEW_CUSTOMER[0]}
        for database, tenant in databases.items():
            digest = copy_digest(app, database, tenant)
            if any(digest.values()):
                failures.append(
                    self.fail(
                        "customer_copy_wrong",
                        database=database,
                        **{k: v[:20] for k, v in digest.items()},
                        counts={k: len(v) for k, v in digest.items()},
                    )
                )
        streaming = app.customer(
            "postgres",
            "select s.subname || '|' || s.subenabled || '|' || coalesce(w.pid::text, '') "
            "from pg_subscription s left join pg_stat_subscription w "
            "on w.subid = s.oid and w.relid is null;",
        )
        for line in streaming.split():
            name, enabled, pid = line.split("|")
            if enabled not in ("t", "true") or not pid:
                failures.append(self.fail("subscription_not_streaming", subscription=name))
        errors_after = self.subscription_errors()
        grown = {
            n: errors_after[n] - errors_before.get(n, 0)
            for n in errors_after
            if errors_after[n] > errors_before.get(n, 0)
        }
        if grown:
            failures.append(self.fail("subscription_not_streaming", apply_errors=grown))
        changed = set(
            app.customer(
                RECEIPT_DATABASE,
                "select r.event_id from handoff.receipts r left join usage_events u using (event_id) "
                "where u.export_receipt is distinct from r.export_receipt;",
            ).split()
        )
        larkspur = set(app.core("select event_id from usage_events where tenant_id = 'larkspur';").split())
        lost = sorted(changed & larkspur)
        if lost:
            failures.append(self.fail("receipts_lost", events=lost[:20], count=len(lost)))
        return failures

    def check_ledgers(self):
        app = self.app
        failures = []
        truth = truth_lines(app)
        billing = ledger(app.billing)
        wrong = differences({k: v for k, v in truth.items()}, billing)
        if wrong:
            failures.append(self.fail("billing_ledger_wrong", lines=wrong[:20], count=len(wrong)))
        target = {(line, account): amount for line, (account, amount) in truth.items()}
        billed = defaultdict(Decimal)
        usage_items = defaultdict(int)
        rows = app.finance(
            "select coalesce(i.line_id::text, '') || '|' || v.account || '|' || i.kind || '|' || i.amount "
            "from invoice_items i join invoices v using (invoice_id);"
        )
        for line, account, kind, amount in (r.split("|") for r in rows.split()):
            if line:
                billed[(int(line), account)] += Decimal(amount)
                if kind == "usage":
                    usage_items[int(line)] += 1
        wrong = differences(target, billed)
        if wrong:
            failures.append(self.fail("finance_ledger_wrong", lines=[list(k) for k in wrong[:20]], count=len(wrong)))
        twice = sorted(line for line, n in usage_items.items() if n > 1)
        uninvoiced = app.finance("select count(*) from ledger_lines where invoice_id is null;").strip()
        if twice or uninvoiced != "0":
            failures.append(
                self.fail("usage_invoiced_twice", lines=twice[:20], count=len(twice), uninvoiced=uninvoiced)
            )
        handoff = self.problem.load_state().get("handoff_at")
        if handoff:
            rows = app.finance(
                "select line_id || '|' || account || '|' || amount from ledger_adjustments "
                f"where line_id is not null and booked_at > {sql_literal(handoff)}::timestamptz;"
            )
            booked = defaultdict(list)
            for line, account, amount in (r.split("|") for r in rows.split()):
                booked[(int(line), account)].append(Decimal(amount))
            noise = [
                list(k)
                for k, amounts in booked.items()
                if sum(amounts) == 0 or (any(a > 0 for a in amounts) and any(a < 0 for a in amounts))
            ]
            if noise:
                failures.append(self.fail("notices_without_change", lines=noise[:20], count=len(noise)))
        owed = defaultdict(Decimal)
        for account, amount in truth.values():
            owed[account] += amount
        charged = defaultdict(Decimal)
        rows = app.finance(
            "select v.account || '|' || sum(i.amount) from invoice_items i join invoices v using (invoice_id) "
            "group by v.account union all select account || '|' || -sum(amount) from credit_notes group by account;"
        )
        for account, amount in (r.split("|") for r in rows.split()):
            charged[account] += Decimal(amount)
        accounts = set(TENANTS) | {NEW_CUSTOMER[0]} | set(charged)
        off = {a: str(charged[a] - owed[a]) for a in sorted(accounts) if abs(charged[a] - owed[a]) > CENT}
        if off:
            failures.append(self.fail("charges_wrong", accounts=off))
        return failures

    def check_ingest(self):
        app = self.app
        rows = app.core(
            "with stored as (select batch_id, count(*) n from usage_events group by batch_id), "
            "voided as (select (before->>'batch_id')::bigint batch_id, count(*) n from usage_corrections "
            "where kind = 'void' group by 1) "
            "select b.batch_id || '|' || coalesce(b.records, -1) || '|' || coalesce(s.n, 0) + coalesce(v.n, 0) "
            "from ingest_batches b left join stored s using (batch_id) left join voided v using (batch_id) "
            "where coalesce(b.records, -1) <> coalesce(s.n, 0) + coalesce(v.n, 0);"
        )
        if rows.strip():
            return [self.fail("ingest_wrong", batches=rows.split()[:20], count=len(rows.split()))]
        return []

    # ------------------------------------------------------------ evaluate
    def evaluate(self, solution=None, trace=None, duration=None):
        app = self.app
        try:
            for pod in ("core-0", "billing-0"):
                app.wait_pod_ready(pod, timeout=180)
            app.wait_pod_ready(f"{SUBSCRIBERS}-0", hidden=True, timeout=180)
        except Exception as exc:  # noqa: BLE001
            return self.fail("routine_jobs_failed", error=f"database unavailable: {exc}")
        try:
            self.onboard_new_customer()
            self.fresh_corrections()
        except Exception as exc:  # noqa: BLE001
            print(f"❌ tonight's routine jobs failed: {exc}")
            return self.fail("routine_jobs_failed", error=str(exc)[:2000])
        failures = []
        try:
            with contextlib.suppress(TimeoutError):
                app.wait_caught_up(timeout=240)
            errors_before = self.subscription_errors()
            app.set_rate(0)
            try:
                app.wait_inbox_empty(timeout=300)
                app.command("rollout", "status", "statefulset/ingest", "--timeout=60s", timeout=90)
            except (TimeoutError, subprocess.CalledProcessError) as exc:
                failures.append(self.fail("ingest_unavailable", error=str(exc)[:500]))
            with contextlib.suppress(TimeoutError):
                app.wait_caught_up(timeout=240)
            failures += self.wait_ledgers_delivered()
            try:
                app.invoice_run([*TENANTS, NEW_CUSTOMER[0]], f"invoice-run-{int(time.time())}")
            except Exception as exc:  # noqa: BLE001
                failures.append(self.fail("routine_jobs_failed", error=f"invoice run: {exc}"[:2000]))
            failures += self.check_readers(errors_before)
            failures += self.check_ledgers()
            failures += self.check_ingest()
        except Exception as exc:  # noqa: BLE001
            print(f"❌ grading raised: {exc}")
            return self.fail_from_exception(exc)
        finally:
            with contextlib.suppress(Exception):
                app.set_rate(60)
        for failure in failures:
            print(f"❌ {failure['reason']}: {failure.get('detail')}")
        if failures:
            first = failures[0]
            return {**first, "detail": {"failures": [{"reason": f["reason"], **f.get("detail", {})} for f in failures]}}
        print("✅ every reader, ledger and charge matches the truth")
        return {"success": True}
