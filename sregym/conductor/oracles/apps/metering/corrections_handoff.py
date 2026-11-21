"""End-state grading for the usage corrections handoff.

After the agent finishes, the company's and its customers' routine work runs as
it would tonight: Vale's site comes back, pubsync onboards a new customer from
the declared configuration, support makes fresh corrections, the nightly
correction files go out if they still do and Pell's loader applies every ready
file in its own copy of the files, traffic settles, billing's daily re-rate
runs, finance imports pending statement returns and invoices each account, and every customer closes its
statement. Then every reader is compared with the truth on
core: each customer copy and statement, billing's ledger, finance's ledger
under its own closed-period rule, the documents already issued, and what each
customer was charged in total.

Checks that tonight's routine cannot change are read first. Failures there
are reported together with failures from tonight's routine and its checks.
"""

import contextlib
import json
import secrets
import time
from collections import defaultdict
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from sregym.conductor.oracles.base import Oracle
from sregym.service.apps.metering.application import (
    COMMITTED_DISCOUNTS,
    ExtractDeliveryTimeout,
    MeteringJobFailed,
    CUSTOMER_DATABASES,
    PRICES,
    SUBSCRIBERS,
    TENANTS,
    parse_extracts_conf,
    sql_literal,
)

COLUMNS = "record_id, tenant_id, metric, quantity::text, occurred_at::text, batch_id, billing_period"
NEW_CUSTOMER = ("wren", "Wren Studios", "wren_usage")
CENT = Decimal("0.01")
VALE = "vale_usage"
BOOKING = "billing_period, tenant_id, metric, quantity"


def statement_truth(app, tenant, owners=None):
    """The intended booking values, keyed by event."""
    owner = "tenant_id"
    if owners:
        owner = "case event_id " + " ".join(
            f"when {int(event)} then {sql_literal(account)}" for event, account in owners.items()
        ) + " else tenant_id end"
    return {int(event): values for event, values in digest_rows(app.core(
        "select event_id || '|' || concat_ws('|', billing_period, account, metric, quantity) "
        f"from (select event_id, billing_period, {owner} as account, metric, quantity from usage_events) u "
        f"where account = {sql_literal(tenant)};"
    )).items()}


def statement_differences(truth, active, preserved):
    """Find bad active bookings and replacements of bookings that were already correct."""
    wrong = set(truth) ^ set(active)
    wrong.update(event for event in truth.keys() & active.keys()
                 if len(active[event]) != 1 or active[event][0][1] != truth[event])
    replaced = {event for event, line, values in preserved
                if truth.get(event) == values and (line, values) not in active.get(event, ())}
    return sorted(wrong), sorted(replaced)


def digest_rows(text):
    return dict(line.split("|", 1) for line in text.split())


def copy_digest(app, database, tenant):
    """Rows a customer copy has that core does not, lacks, or holds with other values."""
    row = f"md5(concat_ws('|', {COLUMNS}))"
    where = f"where tenant_id = {sql_literal(tenant)}"
    # Quill can legitimately store rows with the same event ID in different periods.
    if database == "quill_usage":
        copy_sql = f"select event_id || '|' || string_agg({row}, ',' order by billing_period) from usage_events"
        copy_sql += " group by event_id;"
    else:
        copy_sql = f"select event_id || '|' || {row} from usage_events;"
    truth = digest_rows(app.core(f"select event_id || '|' || {row} from usage_events {where};"))
    copy = digest_rows(app.customer(database, copy_sql))
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


def ledger_rates(run):
    rows = run("select line_id || '|' || rate::text from ledger_lines;")
    return {int(e): rate for e, rate in digest_rows(rows).items()}


def parse_rate(rate):
    """Read the numeric price and band from either version of the rate snapshot."""
    if not rate.startswith("(") or not rate.endswith(")"):
        return None
    price, _, band = rate[1:-1].partition(",")
    try:
        unit_price = Decimal(price)
    except InvalidOperation:
        return None
    if not unit_price.is_finite():
        return None
    return unit_price, "" if band == '""' else band


def rate_differences(billing, finance):
    wrong = set()
    for line, rate in billing.items():
        received = parse_rate(finance.get(line, ""))
        if received is None or received != parse_rate(rate):
            wrong.add(line)
    return wrong


def rate_band(rate):
    parsed = parse_rate(rate)
    return parsed[1] if parsed is not None else ""


def usage_charge(account, amount, rate):
    discount = COMMITTED_DISCOUNTS.get(account, 0)
    if discount and rate_band(rate) == "committed":
        return (amount * (100 - discount) / 100).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
    return amount


def finance_effective(app):
    """The usage and all line-linked adjustments finance will bill for each line and account."""
    effective = defaultdict(Decimal)
    rows = app.finance(
        "select l.line_id || '|' || l.account || '|' || "
        "coalesce(i.amount, usage_charge(l.amount, l.rate, a.committed_discount)) "
        "from ledger_lines l join accounts a using (account) left join invoice_items i "
        "on i.invoice_id = l.invoice_id and i.line_id = l.line_id and i.kind = 'usage' "
        "union all select line_id || '|' || account || '|' || amount from ledger_adjustments "
        "where line_id is not null;"
    )
    for e, a, x in (line.split("|") for line in rows.split()):
        effective[(int(e), a)] += Decimal(x)
    return effective


def differences(expected, actual):
    keys = set(expected) | set(actual)
    return sorted(k for k in keys if expected.get(k, Decimal(0)) != actual.get(k, Decimal(0)))


def finance_differences(app):
    rates = ledger_rates(app.billing)
    target = {
        (line, account): usage_charge(account, amount, rates.get(line, ""))
        for line, (account, amount) in ledger(app.billing).items()
    }
    return differences(target, finance_effective(app))


def documents(app):
    """A digest of every document issued so far: invoices with their items, credit notes, captures, refunds."""
    finance = app.finance(
        "select 'invoice:' || v.invoice_id || '|' || md5(row(v.*)::text || coalesce((select string_agg(row(i.*)::text, ',' "
        "order by i.kind, i.ref, i.line_id) from invoice_items i where i.invoice_id = v.invoice_id), '')) from invoices v "
        "union all select 'credit:' || c.credit_id || '|' || md5(row(c.*)::text) from credit_notes c;"
    )
    payments = app.core(
        "select 'capture:' || c.capture_id || '|' || md5(row(c.*)::text) from captures c "
        "union all select 'refund:' || r.refund_id || '|' || md5(row(r.*)::text) from refunds r;",
        database="payments",
    )
    return digest_rows(finance + "\n" + payments)


def changed_documents(before, after):
    return sorted(k for k, v in before.items() if after.get(k) != v)


def notice_noise(booked):
    """Find line/account adjustments whose combined financial effect is zero."""
    return sorted(key for key, amounts in booked.items() if sum(amounts) == 0)


def charges_off(owed, charged, accounts):
    differences = {}
    for account in sorted(accounts):
        delta = round_charge(charged.get(account, Decimal(0))) - round_charge(owed.get(account, Decimal(0)))
        if delta:
            differences[account] = str(delta)
    return differences


def round_charge(amount):
    return amount.quantize(CENT, rounding=ROUND_HALF_UP)


class MeteringOracle(Oracle):
    evaluation_timeout_seconds = 2400
    # The validator needs the reference repair to start before tonight's jobs.
    validation_requires_fresh_recovery = True
    FAILURE_CLASSES = {
        "usage_entered_twice": "agent",
        "duplicate_usage_missing": "agent",
        "duplicate_usage_changed": "agent",
        "documents_changed": "agent",
        "customer_copy_wrong": "agent",
        "core_usage_wrong": "agent",
        "statement_reversed": "agent",
        "statement_rebooked": "agent",
        "statement_lines_wrong": "agent",
        "statement_return_not_imported": "agent",
        "statement_return_waiver_wrong": "agent",
        "subscription_not_streaming": "agent",
        "billing_ledger_wrong": "agent",
        "finance_ledger_wrong": "agent",
        "finance_rates_wrong": "agent",
        "invoice_rates_wrong": "agent",
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

    def state(self):
        return self.problem.load_state()

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
                f"insert into accounts (account, name, billing_day, email) "
                f"values ('{tenant}', {sql_literal(name)}, 26, 'billing@{tenant}.example') "
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
            app.create_customer_database(database)
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
            ("pell", "void", None, None),
            ("pell", "reduce", None, None),
            ("cinder", "move", "pell", None),
            ("quill", "reduce", None, None),
            ("larkspur", "move", "ostrava", None),
            ("arden", "move", "larkspur", None),
            ("vale", "move", "wren", None),
            ("larkspur", "reduce", None, None),
            ("garnet", "void", None, True),
        ]
        return problem.corrections(plan)

    def nightly_files(self):
        """Run tonight's correction extracts for the accounts still configured."""
        app = self.app
        data = app.command("get", "cronjob", "correction-extracts", "--ignore-not-found", "-o", "json")
        if not data.strip() or json.loads(data)["spec"].get("suspend"):
            return
        conf = json.loads(app.command("get", "configmap", "correction-extracts", "-o", "json"))["data"]
        if parse_extracts_conf(conf.get("extracts.conf", "")):
            app.run_cronjob("correction-extracts", f"correction-extracts-{int(time.time())}")

    def wait_ledgers_delivered(self, timeout=900):
        """Billing has every usage change from core, and finance every billing change, before invoicing."""
        app = self.app
        deadline = time.monotonic() + timeout
        try:
            app.wait_caught_up(slots=["billing_usage"], timeout=timeout)
        except TimeoutError as exc:
            return [self.fail("billing_ledger_wrong", error=str(exc)[:500])]
        with contextlib.suppress(TimeoutError):
            app.wait_caught_up(app.billing, slots=["finance_ledger"], timeout=timeout)
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

    def lagging_feeds(self, databases, timeout=90):
        """Customer feeds whose slot on core is not streaming up to the current position."""
        app = self.app
        target = app.core("select pg_current_wal_lsn();").strip()
        names = ",".join(sql_literal(db) for db in databases)
        deadline = time.monotonic() + timeout
        while True:
            behind = app.core(
                f"select string_agg(n.name, ' ') from unnest(array[{names}]) n(name) left join pg_replication_slots s "
                f"on s.slot_name = n.name where s.slot_name is null or not s.active or s.confirmed_flush_lsn is null "
                f"or s.confirmed_flush_lsn < {sql_literal(target)}::pg_lsn;"
            ).strip()
            if not behind or time.monotonic() > deadline:
                return behind.split()
            time.sleep(3)

    def check_duplicate_usage(self):
        state = self.state()
        if "w" not in state:
            return []
        original, rekeyed = state["w"], state["w2"]
        rows = self.app.rows(
            self.app.core,
            "select row_to_json(u) from (select tenant_id, metric, quantity, occurred_at from usage_events "
            f"where event_id in ({original}, {rekeyed})) u;",
        )
        if len(rows) > 1:
            return [self.fail("usage_entered_twice", events=[original, rekeyed])]
        if not rows:
            return [self.fail("duplicate_usage_missing", events=[original, rekeyed])]
        expected = state.get("duplicate_usage")
        if expected is not None and rows[0] != expected:
            return [self.fail("duplicate_usage_changed", expected=expected, actual=rows[0])]
        return []

    def check_core_usage(self):
        """The usage rows the complaint batches touched, on core, against what each row really is."""
        truth = (self.state().get("complaints") or {}).get("truth")
        if not truth:
            return []
        rows = {
            str(r["event_id"]): {k: r[k] for k in ("tenant_id", "metric", "quantity", "occurred_at")}
            for r in self.app.rows(
                self.app.core,
                "select row_to_json(u) from (select event_id, tenant_id, metric, quantity, occurred_at from usage_events "
                f"where event_id in ({','.join(truth)})) u;",
            )
        }
        wrong = sorted(int(e) for e, row in truth.items() if rows.get(e) != row)
        return [self.fail("core_usage_wrong", events=wrong[:20], count=len(wrong))] if wrong else []

    def check_documents(self):
        before = self.state().get("documents") or {}
        changed = changed_documents(before, documents(self.app))
        if changed:
            return [self.fail("documents_changed", documents=changed[:20], count=len(changed))]
        return []

    def check_readers(self, excluded=()):
        app = self.app
        failures = []
        databases = {**CUSTOMER_DATABASES, NEW_CUSTOMER[2]: NEW_CUSTOMER[0]}
        databases = {database: tenant for database, tenant in databases.items() if database not in excluded}
        if not databases:
            return failures
        lagging = set(self.lagging_feeds(list(databases)))
        disabled = app.customer("postgres", "select subname from pg_subscription where not subenabled;").split()
        lagging.update(set(disabled) & databases.keys())
        if lagging:
            failures.append(self.fail("subscription_not_streaming", feeds=sorted(lagging)))
        for database, tenant in databases.items():
            try:
                digest = copy_digest(app, database, tenant)
            except Exception as exc:  # noqa: BLE001
                failures.append(self.fail_from_exception(exc, database=database))
                continue
            if any(digest.values()):
                failures.append(
                    self.fail(
                        "customer_copy_wrong",
                        database=database,
                        **{k: v[:20] for k, v in digest.items()},
                        counts={k: len(v) for k, v in digest.items()},
                    )
                )
        return failures

    def check_statements(self, database, close):
        app = self.app
        tenant = CUSTOMER_DATABASES.get(database, NEW_CUSTOMER[0])
        truth = statement_truth(app, tenant)
        active = defaultdict(list)
        rows = app.customer(
            database,
            f"select event_id || '|' || line_id || '|' || concat_ws('|', {BOOKING}) from statement_lines s "
            "where kind = 'line' and not exists (select 1 from statement_lines r "
            "where r.line_id = s.line_id and r.kind = 'reversal');",
        )
        for event, line, values in (row.split("|", 2) for row in rows.splitlines()):
            active[int(event)].append((int(line), values))
        preserved = []
        reversed_lines = set()
        if database in CUSTOMER_DATABASES:
            rows = app.customer(
                database,
                "select s.event_id || '|' || s.line_id || '|' || s.booking || '|' || "
                "exists (select 1 from statement_lines r where r.line_id = s.line_id "
                f"and r.kind = 'reversal' and r.close_id = {close}) from handoff.sealed s;",
            )
            for row in rows.splitlines():
                booking, reversed_now = row.rsplit("|", 1)
                event, line, values = booking.split("|", 2)
                preserved.append((int(event), int(line), values))
                if reversed_now == "true" and truth.get(int(event)) == values:
                    reversed_lines.add(int(event))
        wrong, rebooked = statement_differences(truth, active, preserved)
        return [
            self.fail(reason, database=database, events=events[:20], count=len(events))
            for reason, events in (
                ("statement_reversed", sorted(reversed_lines)),
                ("statement_lines_wrong", wrong),
                ("statement_rebooked", rebooked),
            ) if events
        ]

    def check_ledgers(self, invoice_boundary):
        app = self.app
        failures = []
        truth = truth_lines(app)
        billing = ledger(app.billing)
        wrong = differences(dict(truth), billing)
        if wrong:
            failures.append(self.fail("billing_ledger_wrong", lines=wrong[:20], count=len(wrong)))
        rates = self.expected_rates()
        state = self.state()
        waived = {state["statement_return"]["genuine"]}
        waived.update(row["line"] for row in state.get("allocation_returns", {}).get("genuine", []))
        target = {
            (line, account): usage_charge(account, amount, rates.get(line, ""))
            for line, (account, amount) in truth.items()
            if line not in waived
        }
        billed = defaultdict(Decimal)
        usage_items = defaultdict(int)
        rows = app.finance(
            "select i.line_id || '|' || v.account || '|' || i.kind || '|' || i.amount || '|' || i.invoice_id "
            "from invoice_items i join invoices v using (invoice_id) "
            "where i.line_id is not null;"
        )
        invoice_rates_wrong = set()
        for line, account, kind, amount, invoice in (r.split("|") for r in rows.split()):
            key = (int(line), account)
            amount = Decimal(amount)
            billed[key] += amount
            if kind == "usage":
                usage_items[int(line)] += 1
                if invoice_boundary is not None and int(invoice) > invoice_boundary and amount != target.get(key):
                    invoice_rates_wrong.add(int(line))
        if invoice_rates_wrong:
            failures.append(
                self.fail("invoice_rates_wrong", lines=sorted(invoice_rates_wrong)[:20], count=len(invoice_rates_wrong))
            )
        wrong = differences(target, billed)
        if wrong:
            failures.append(self.fail("finance_ledger_wrong", lines=[list(k) for k in wrong[:20]], count=len(wrong)))
        twice = sorted(line for line, n in usage_items.items() if n > 1)
        uninvoiced = app.finance(
            "select count(*) from ledger_lines where invoice_id is null and line_id not in "
            f"({','.join(map(str, sorted(waived)))});"
        ).strip()
        if twice or uninvoiced != "0":
            failures.append(
                self.fail("usage_invoiced_twice", lines=twice[:20], count=len(twice), uninvoiced=uninvoiced)
            )
        handoff = self.state().get("handoff_at")
        if handoff:
            rows = app.finance(
                "select line_id || '|' || account || '|' || amount from ledger_adjustments "
                f"where line_id is not null and booked_at > {sql_literal(handoff)}::timestamptz;"
            )
            booked = defaultdict(list)
            for line, account, amount in (r.split("|") for r in rows.split()):
                booked[(int(line), account)].append(Decimal(amount))
            noise = notice_noise(booked)
            if noise:
                failures.append(
                    self.fail("notices_without_change", lines=[list(k) for k in noise[:20]], count=len(noise))
                )
        return failures + self.check_charges(target)

    def check_charges(self, target):
        app = self.app
        owed = defaultdict(Decimal)
        for (_line, account), amount in target.items():
            owed[account] += amount
        charged = defaultdict(Decimal)
        bad_totals = []
        stored_invoices = {}
        invoices = app.finance("""
            select v.invoice_id || '|' || v.account || '|' || v.total || '|' || coalesce(sum(i.amount), 0)
            from invoices v left join invoice_items i using (invoice_id) group by v.invoice_id;
        """)
        for invoice, account, stored, items in (r.split("|") for r in invoices.split()):
            total, item_sum = Decimal(stored), Decimal(items)
            rounded = round_charge(item_sum)
            if total != rounded:
                bad_totals.append(int(invoice))
            stored_invoices[int(invoice)] = (account, total)
            charged[account] += total
            owed[account] += rounded - item_sum
        rows = (
            app.finance("select account || '|' || -sum(amount) from credit_notes group by account;")
            + "\n"
            + app.core(
                "select c.account || '|' || -sum(r.amount) from refunds r join captures c using (capture_id) group by c.account;",
                database="payments",
            )
        )
        for account, amount in (r.split("|") for r in rows.split()):
            charged[account] += Decimal(amount)
        off = charges_off(owed, charged, set(TENANTS) | {NEW_CUSTOMER[0]} | set(charged))
        captures = app.core(
            "select capture_id || '|' || invoice_id || '|' || account || '|' || amount from captures;",
            database="payments",
        )
        bad_captures = [
            int(capture) for capture, invoice, account, amount in (row.split("|") for row in captures.split())
            if stored_invoices.get(int(invoice)) != (account, Decimal(amount))
        ]
        if off or bad_totals or bad_captures:
            return [self.fail("charges_wrong", accounts=off, invoices=sorted(bad_totals)[:20],
                              captures=sorted(bad_captures)[:20])]
        return []

    def expected_rates(self):
        """Rate Core usage by its sealed cohort, with current rates for new events."""
        sealed = {
            event: cohort
            for cohort, ranges in self.state()["rate_cohorts"].items()
            for first, last in ranges
            for event in range(first, last + 1)
        }
        rows = self.app.core("select event_id || '|' || metric from usage_events;")
        expected = {}
        for row in rows.splitlines():
            line, metric = row.split("|", 1)
            event = int(line)
            band = ""
            if sealed.get(event) != "legacy":
                # Both r5 cohorts and new usage follow the same band rules.
                band = "list" if metric == "storage_gb_h" else "committed"
            expected[event] = f"({PRICES.get(metric, '')},{band})"
        return expected

    def check_rates(self):
        expected = self.expected_rates()
        billing = ledger_rates(self.app.billing)
        finance = ledger_rates(self.app.finance)
        # Closed voids remain in finance with their original invoice and offset.
        wrong = sorted(rate_differences(expected, finance) | rate_differences(expected, billing))
        return [self.fail("finance_rates_wrong", lines=wrong[:20], count=len(wrong))] if wrong else []

    def check_statement_return(self):
        app = self.app
        returned = self.state()["statement_return"]
        genuine, missing = returned["genuine"], returned["missing"]
        failures = []
        decisions = {
            r["event"]: ((r["waived"] == returned["ref"] if r["event"] == genuine else r["waived"] is not None), r["charged"])
            for r in app.rows(
                app.finance,
                "select json_build_object('event', l.line_id, 'waived', l.waived_by_return, "
                "'charged', exists (select 1 from invoice_items i join invoices v using (invoice_id) "
                "where i.line_id = l.line_id and i.kind = 'usage' and v.account = 'pell')) "
                f"from ledger_lines l where l.account = 'pell' and l.line_id in ({genuine}, {missing});",
            )
        }
        for event, expected in ((genuine, (True, False)), (missing, (False, True))):
            if decisions.get(event) != expected:
                failures.append(self.fail("statement_return_waiver_wrong", event=event, decision=decisions.get(event)))
        history = app.customer(
            "pell_usage", f"select kind from statement_lines where event_id = {missing};"
        ).split()
        if history != ["line"]:
            failures.append(self.fail("statement_lines_wrong", database="pell_usage", event=missing, history=history))
        return failures

    def check_return_reconciliations(self, resend_boundary=None):
        app = self.app
        state = self.state()
        returns = {(row["account"], row["ref"]): row for row in state["signed_returns"]}
        records = {
            (r["account"], r["ref"]): r
            for r in app.rows(app.finance, """
                select json_build_object('account', account, 'ref', return_ref, 'digest', statement_digest)
                from statement_return_reconciliations;
            """)
        }
        later = f"or delivery_id > {int(resend_boundary)}" if resend_boundary is not None else ""
        acknowledgements = set(tuple(row.split("|")) for row in app.core(
            "select account || '|' || return_ref from statement_return_acknowledgements;", database="support"
        ).splitlines())
        pending = set(tuple(row.split("|")) for row in app.core(
            "select account || '|' || return_ref from statement_return_inbox "
            f"where imported_at is null {later};", database="support"
        ).splitlines())
        missing = []
        for key, signed in returns.items():
            record = records.get(key)
            if (key not in acknowledgements or key in pending or record is None
                    or record["digest"] != signed["digest"]):
                missing.append(key)
        return [self.fail("statement_return_not_imported", returns=sorted({ref for _, ref in missing}),
                          account_returns=sorted(missing))] if missing else []

    def check_allocation_returns(self):
        app = self.app
        allocation = self.state().get("allocation_returns")
        if not allocation:
            return []
        failures = []
        earned = allocation["earned"]
        genuine = allocation["genuine"]
        ids = ",".join(str(row["line"]) for row in earned + genuine)
        decisions = app.rows(app.finance, f"""
            select json_build_object('line', l.line_id, 'account', l.account, 'waiver', l.waived_by_return,
                                     'charges', coalesce(sum(i.amount), 0), 'items', count(i.invoice_id),
                                     'billed_account', min(v.account))
            from ledger_lines l
            left join invoice_items i on i.line_id = l.line_id and i.kind = 'usage'
            left join invoices v on v.invoice_id = i.invoice_id
            where l.line_id in ({ids})
            group by l.line_id;
        """)
        by_line = {row["line"]: row for row in decisions}
        allowed = {(row["line"], row["account"], row["ref"]) for row in genuine}
        pell = self.state()["statement_return"]
        allowed.add((pell["genuine"], "pell", pell["ref"]))
        waived = app.rows(app.finance, """
            select json_build_object('line', line_id, 'account', account, 'waiver', waived_by_return)
            from ledger_lines where waived_by_return is not null;
        """)
        for row in waived:
            key = (row["line"], row["account"], row["waiver"])
            if key not in allowed and row["line"] not in by_line and row["line"] != pell["missing"]:
                failures.append(self.fail("statement_return_waiver_wrong", event=row["line"], decision=row))
        for row in earned + genuine:
            actual = by_line.get(row["line"])
            expected = (row["account"], row.get("amount"), None, row["account"], 1) if "amount" in row else (
                row["account"], 0, row["ref"], None, 0
            )
            if not actual or tuple(actual[k] for k in ("account", "charges", "waiver", "billed_account", "items")) != expected:
                failures.append(self.fail("statement_return_waiver_wrong", event=row["line"], decision=actual))
        recorded = app.rows(app.finance, """
            select json_build_object('line', line, 'account', r.account, 'waiver', r.return_ref)
            from statement_return_reconciliations r cross join lateral unnest(r.waived_lines) as lines(line)
            where not exists (
                select 1 from statement_waiver_revocations v
                where v.account = r.account and v.return_ref = r.return_ref and v.line_id = lines.line
            );
        """)
        actual_decisions = {(r["line"], r["account"], r["waiver"]) for r in recorded}
        already_failed = {f["detail"]["event"] for f in failures}
        for line, account, ref in sorted(actual_decisions ^ allowed):
            if line not in already_failed:
                failures.append(self.fail("statement_return_waiver_wrong", event=line,
                                          decision={"account": account, "return": ref,
                                                    "recorded_waiver": (line, account, ref) in actual_decisions}))
        return failures

    def check_ingest(self):
        app = self.app
        rows = app.core(
            "with stored as (select batch_id, count(*) n from usage_events group by batch_id), "
            "voided as (select (before->>'batch_id')::bigint batch_id, count(*) n from usage_corrections "
            "where kind = 'void' group by 1) "
            "select b.batch_id || '|' || coalesce(b.records, -1) || '|' || coalesce(s.n, 0) + coalesce(v.n, 0) "
            "from ingest_batches b left join stored s using (batch_id) left join voided v using (batch_id) "
            # Usage keyed in by support is not an ingest batch; removing a duplicate of it is a valid repair.
            "where b.source <> 'support-console' and coalesce(b.records, -1) <> coalesce(s.n, 0) + coalesce(v.n, 0);"
        )
        if rows.strip():
            return [self.fail("ingest_wrong", batches=rows.split()[:20], count=len(rows.split()))]
        return []

    # ------------------------------------------------------------ evaluate
    def observe(self):
        """Read-only notes on the end state before tonight's routine, kept with the result for analysis (not graded)."""
        app = self.app
        notes = {}
        with contextlib.suppress(Exception):
            notes["copies"] = {
                db: {k: len(v) for k, v in copy_digest(app, db, tenant).items()}
                for db, tenant in CUSTOMER_DATABASES.items()
            }
        with contextlib.suppress(Exception):
            notes["finance_gaps"] = len(finance_differences(app))
        with contextlib.suppress(Exception):
            notes["slots_active"] = dict(
                line.split("|")
                for line in app.core(
                    "select slot_name || '|' || active from pg_replication_slots where slot_type = 'logical';"
                ).split()
            )
        with contextlib.suppress(Exception):
            notes["pell_ready_files"] = app.customer(
                "pell_usage",
                "select count(*) from extract_files e where status = 'ready' "
                "and not exists (select 1 from loaded_files l where l.file_id = e.file_id);",
            ).strip()
        with contextlib.suppress(Exception):
            job = json.loads(app.command("get", "cronjob", "correction-extracts", "-o", "json"))
            conf = json.loads(app.command("get", "configmap", "correction-extracts", "-o", "json"))["data"]
            notes["nightly_files"] = {
                "suspended": bool(job["spec"].get("suspend")),
                "accounts": sorted(parse_extracts_conf(conf.get("extracts.conf", ""))),
            }
        return notes

    def report(self, failures, observed=None):
        for failure in failures:
            print(f"❌ {failure['reason']}: {failure.get('detail')}")
        first = next((f for f in failures if f["failure_class"] == "agent"), failures[0])
        detail = {"failures": [{"reason": f["reason"], **f.get("detail", {})} for f in failures]}
        if observed is not None:
            detail["observed"] = observed
        return {**first, "detail": detail}

    def evaluate(self, solution=None, trace=None, duration=None):
        app = self.app
        for name, customer_side in (("core", False), ("billing", False), (SUBSCRIBERS, True)):
            try:
                app.wait_pod_ready(f"{name}-0", hidden=customer_side, timeout=180)
            except Exception as exc:  # noqa: BLE001
                if not customer_side and isinstance(exc, TimeoutError):
                    with contextlib.suppress(Exception):
                        data = app.command("get", "statefulset", name, "--ignore-not-found", "-o", "json")
                        if not data.strip() or not json.loads(data)["status"].get("readyReplicas", 0):
                            return self.fail("routine_jobs_failed", step="database readiness", database=name)
                return self.fail_from_exception(exc, step="database readiness", database=name)

        failures = []

        def run(step, action, company_job=False):
            try:
                return action()
            except Exception as exc:  # noqa: BLE001
                if isinstance(exc, ExtractDeliveryTimeout) or (company_job and isinstance(exc, MeteringJobFailed)):
                    failure = self.fail("routine_jobs_failed", step=step, error=str(exc)[:2000])
                else:
                    failure = self.fail_from_exception(exc, step=step)
                failures.append(failure)
                return None

        def completed(step, action, company_job=False):
            def finish():
                action()
                return True

            return run(step, finish, company_job)

        def check(action, *args):
            for failure in run(action.__name__, lambda: action(*args)) or ():
                if failure not in failures:
                    failures.append(failure)

        def wait_feeds():
            disabled = app.customer("postgres", "select subname from pg_subscription where not subenabled;").split()
            with contextlib.suppress(TimeoutError):
                app.wait_caught_up(timeout=120, skip=disabled)

        def cron(name, *, customer_side=False, suffix=""):
            return app.run_cronjob(name, f"{name}-{int(time.time())}{suffix}", customer_side=customer_side)

        def return_boundary():
            return int(app.core("select coalesce(max(delivery_id), 0) from statement_return_inbox;", database="support"))

        observed = self.observe()
        print(f"observed before tonight's routine: {json.dumps(observed)}")
        with contextlib.suppress(Exception):
            observed["subscription_errors_before"] = self.subscription_errors()
        check(self.check_duplicate_usage)
        check(self.check_core_usage)
        check(self.check_documents)
        try:
            excluded = set()
            if not completed("Vale returns", lambda: app.customer(VALE, f"alter subscription {VALE} enable;")):
                excluded.add(VALE)
            if not completed("onboarding", self.onboard_new_customer, company_job=True):
                excluded.add(NEW_CUSTOMER[2])
            completed("fresh corrections", self.fresh_corrections)
            run("wait for corrections", wait_feeds)
            completed("nightly extracts", self.nightly_files, company_job=True)
            pell_loaded = completed("Pell file loader", lambda: app.load_extract_files("pell", "pell_usage"))
            if completed("pause ingest", lambda: app.set_rate(0)):
                try:
                    app.wait_inbox_empty(timeout=300)
                except TimeoutError as exc:
                    failures.append(self.fail("ingest_unavailable", step="drain inbox", error=str(exc)))
                except Exception as exc:  # noqa: BLE001
                    failures.append(self.fail_from_exception(exc, step="drain inbox"))
            try:
                app.command("rollout", "status", "statefulset/ingest", "--timeout=60s", timeout=90)
            except Exception as exc:  # noqa: BLE001
                failures.append(self.fail("ingest_unavailable", step="ingest rollout", error=str(exc)[:2000]))
            run("wait for readers", wait_feeds)
            check(self.wait_ledgers_delivered)
            rerated = completed("re-rate", lambda: cron("billing-rerate"), company_job=True)
            check(self.wait_ledgers_delivered)
            boundary = run("invoice boundary", lambda: int(app.finance("select coalesce(max(invoice_id), 0) from invoices;")))
            resent = completed("customer return resend", lambda: cron("statement-return-resend", customer_side=True))
            imported = completed("statement return import", lambda: cron("statement-return-import"), company_job=True)
            if resent and imported:
                cutoff = run("return resend boundary", return_boundary)
                if cutoff is not None and completed("customer return resend after import", lambda: cron(
                    "statement-return-resend", customer_side=True, suffix="-settled"
                )):
                    check(self.check_return_reconciliations, cutoff)
            accounts = [*TENANTS, *([] if NEW_CUSTOMER[2] in excluded else [NEW_CUSTOMER[0]])]
            invoiced = completed("invoice run", lambda: app.invoice_run(
                accounts, f"invoice-run-{int(time.time())}"
            ), company_job=True)
            closed = set()
            for db in [*CUSTOMER_DATABASES, NEW_CUSTOMER[2]]:
                if db in excluded or (db == "pell_usage" and not pell_loaded):
                    continue
                close = run(f"{db} statement close", lambda: app.close_statement(db))
                if close is not None:
                    closed.add(db)
                    check(self.check_statements, db, close)
            check(self.check_duplicate_usage)
            check(self.check_documents)
            check(self.check_readers, excluded)
            if rerated:
                check(self.check_rates)
            if rerated and invoiced and imported and resent:
                check(self.check_ledgers, boundary)
                if "pell_usage" in closed:
                    check(self.check_statement_return)
                check(self.check_allocation_returns)
            check(self.check_ingest)
            with contextlib.suppress(Exception):
                observed["subscription_errors_after"] = self.subscription_errors()
        finally:
            with contextlib.suppress(Exception):
                app.set_rate(60)
        if failures:
            return self.report(failures, observed)
        print("✅ every reader, statement, ledger and charge matches the truth")
        return {"success": True, "detail": {"observed": observed}}
