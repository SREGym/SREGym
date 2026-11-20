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
import subprocess
import time
from collections import defaultdict
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from sregym.conductor.oracles.base import Oracle
from sregym.service.apps.metering.application import (
    COMMITTED_DISCOUNTS,
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
#: Finance's own closed-period rule books only these reasons (finance.sql, hold_invoiced_line).
RULE_SQL = (
    "(reason in ('usage voided after invoice', 'usage re-rated after invoice') "
    "or reason like 'usage moved to %' or reason like 'usage moved from %')"
)
VALE = "vale_usage"


def is_rule_reason(reason):
    return reason in ("usage voided after invoice", "usage re-rated after invoice") or reason.startswith(
        ("usage moved to ", "usage moved from ")
    )


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
    """(line, account) -> what finance will bill for it under its own rule: the line plus the rule's adjustments."""
    effective = defaultdict(Decimal)
    rows = app.finance(
        "select l.line_id || '|' || l.account || '|' || "
        "coalesce(i.amount, usage_charge(l.amount, l.rate, a.committed_discount)) "
        "from ledger_lines l join accounts a using (account) left join invoice_items i "
        "on i.invoice_id = l.invoice_id and i.line_id = l.line_id and i.kind = 'usage' "
        f"union all select line_id || '|' || account || '|' || amount from ledger_adjustments "
        f"where line_id is not null and {RULE_SQL};"
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
    """(line, account) pairs whose rule adjustments since the handoff cancel out or point both ways."""
    return sorted(
        k
        for k, amounts in booked.items()
        if sum(amounts) == 0 or (any(a > 0 for a in amounts) and any(a < 0 for a in amounts))
    )


def charges_off(owed, charged, accounts):
    return {
        a: str(charged.get(a, Decimal(0)) - owed.get(a, Decimal(0)))
        for a in sorted(accounts)
        if abs(charged.get(a, Decimal(0)) - owed.get(a, Decimal(0))) > CENT
    }


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
        """Tonight's correction-extracts run, if the job is still on and still lists Pell."""
        app = self.app
        try:
            job = json.loads(app.command("get", "cronjob", "correction-extracts", "-o", "json"))
            conf = json.loads(app.command("get", "configmap", "correction-extracts", "-o", "json"))["data"]
        except (subprocess.CalledProcessError, KeyError, json.JSONDecodeError):
            return False
        if job["spec"].get("suspend") or "pell" not in parse_extracts_conf(conf.get("extracts.conf", "")):
            return False
        app.run_cronjob("correction-extracts", f"correction-extracts-{int(time.time())}")
        return True

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
                f"on s.slot_name = n.name where s.slot_name is null or not s.active "
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

    def check_documents(self):
        before = self.state().get("documents") or {}
        changed = changed_documents(before, documents(self.app))
        if changed:
            return [self.fail("documents_changed", documents=changed[:20], count=len(changed))]
        return []

    def precheck_feeds(self):
        lagging = self.lagging_feeds([db for db in CUSTOMER_DATABASES if db != VALE])
        return [self.fail("subscription_not_streaming", feeds=lagging)] if lagging else []

    def check_readers(self, errors_before):
        app = self.app
        failures = []
        databases = {**CUSTOMER_DATABASES, NEW_CUSTOMER[2]: NEW_CUSTOMER[0]}
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
        disabled = app.customer(
            "postgres", "select string_agg(subname, ' ') from pg_subscription where not subenabled;"
        )
        lagging = sorted(set(self.lagging_feeds(list(databases))) | set(disabled.split()))
        if lagging:
            failures.append(self.fail("subscription_not_streaming", feeds=lagging))
        if errors_before is not None:
            errors_after = self.subscription_errors()
            grown = {
                n: errors_after[n] - errors_before.get(n, 0)
                for n in errors_after
                if errors_after[n] > errors_before.get(n, 0)
            }
            if grown:
                failures.append(self.fail("subscription_not_streaming", apply_errors=grown))
        return failures

    def check_statements(self, closes, touched):
        """Tonight's statement close books every row once, reverses nothing still true, keeps what was right."""
        app = self.app
        failures = []
        for database, close in closes.items():
            tenant = CUSTOMER_DATABASES.get(database, NEW_CUSTOMER[0])
            truth = {
                int(e)
                for e in app.core(f"select event_id from usage_events where tenant_id = {sql_literal(tenant)};").split()
            }
            stray = ""
            if database == "quill_usage":
                stray = f"and line_id <> {self.state()['quill_period']['entering']['stray_line']}"
            reversed_ = {
                int(e)
                for e in app.customer(
                    database,
                    f"select event_id from statement_lines where kind = 'reversal' and close_id = {close} {stray};",
                ).split()
            }
            wrong = sorted(reversed_ & truth)
            if wrong:
                failures.append(self.fail("statement_reversed", database=database, events=wrong[:20], count=len(wrong)))
            unbooked = app.customer(
                database,
                "select count(*) from usage_events u where not exists (select 1 from statement_lines s "
                "where s.line_id = u.line_id and s.kind = 'line') or exists (select 1 from statement_lines r "
                "where r.line_id = u.line_id and r.kind = 'reversal');",
            ).strip()
            twice = app.customer(
                database,
                "select count(*) from (select event_id from statement_lines l where kind = 'line' and not exists "
                "(select 1 from statement_lines r where r.line_id = l.line_id and r.kind = 'reversal') "
                "group by event_id having count(*) > 1) t;",
            ).strip()
            if unbooked != "0" or twice != "0":
                failures.append(
                    self.fail("statement_lines_wrong", database=database, unbooked=unbooked, booked_twice=twice)
                )
            if database not in CUSTOMER_DATABASES or touched is None:
                continue
            rows = app.customer(
                database,
                "select s.event_id || '|' || s.line_id || '|' || coalesce(u.line_id::text, '') from handoff.sealed s "
                "left join usage_events u using (event_id, billing_period) where s.correct;",
            )
            rebooked = sorted(
                int(e)
                for e, old, new in (r.split("|") for r in rows.split())
                if int(e) in truth and int(e) not in touched and old != new
            )
            if rebooked:
                failures.append(
                    self.fail("statement_rebooked", database=database, events=rebooked[:20], count=len(rebooked))
                )
        return failures

    def check_ledgers(self, invoice_boundary):
        app = self.app
        failures = []
        truth = truth_lines(app)
        billing = ledger(app.billing)
        wrong = differences(dict(truth), billing)
        if wrong:
            failures.append(self.fail("billing_ledger_wrong", lines=wrong[:20], count=len(wrong)))
        rates = ledger_rates(app.billing)
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
            "left join ledger_adjustments a on i.kind = 'adjustment' and a.adjustment_id = i.ref "
            f"where i.line_id is not null and (i.kind = 'usage' or {RULE_SQL.replace('reason', 'a.reason')});"
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
                f"where line_id is not null and {RULE_SQL} and booked_at > {sql_literal(handoff)}::timestamptz;"
            )
            booked = defaultdict(list)
            for line, account, amount in (r.split("|") for r in rows.split()):
                booked[(int(line), account)].append(Decimal(amount))
            noise = notice_noise(booked)
            if noise:
                failures.append(
                    self.fail("notices_without_change", lines=[list(k) for k in noise[:20]], count=len(noise))
                )
        owed = defaultdict(Decimal)
        for (_line, account), amount in target.items():
            owed[account] += amount
        charged = defaultdict(Decimal)
        rows = (
            app.finance(
                "select v.account || '|' || sum(i.amount) from invoice_items i join invoices v using (invoice_id) "
                "group by v.account union all select account || '|' || -sum(amount) from credit_notes group by account;"
            )
            + "\n"
            + app.core(
                "select c.account || '|' || -sum(r.amount) from refunds r join captures c using (capture_id) group by c.account;",
                database="payments",
            )
        )
        for account, amount in (r.split("|") for r in rows.split()):
            charged[account] += Decimal(amount)
        off = charges_off(owed, charged, set(TENANTS) | {NEW_CUSTOMER[0]} | set(charged))
        if off:
            failures.append(self.fail("charges_wrong", accounts=off))
        return failures

    def check_rates(self):
        billing = ledger_rates(self.app.billing)
        finance = ledger_rates(self.app.finance)
        # Closed voids remain in finance with their original invoice and offset.
        wrong = sorted(rate_differences(billing, finance))
        return [self.fail("finance_rates_wrong", lines=wrong[:20], count=len(wrong))] if wrong else []

    def check_statement_return(self):
        app = self.app
        returned = self.state()["statement_return"]
        genuine, missing = returned["genuine"], returned["missing"]
        failures = []
        imported = app.core(
            f"select imported_at is not null from statement_return_inbox where delivery_id = {returned['retry']};",
            database="support",
        ).strip()
        if imported != "t":
            failures.append(self.fail("statement_return_not_imported", delivery=returned["retry"]))
        decisions = {
            r["event"]: (r["waived"], r["charged"])
            for r in app.rows(
                app.finance,
                "select json_build_object('event', l.line_id, 'waived', l.waived_by_return is not null, "
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

    def check_allocation_returns(self, delivery_boundary):
        app = self.app
        allocation = self.state().get("allocation_returns")
        if not allocation:
            return []
        close = sql_literal(allocation["close"])
        later = delivery_boundary if delivery_boundary is not None else 0
        deliveries = app.rows(
            lambda q: app.core(q, database="support"),
            f"""
            select json_build_object('ref', i.return_ref, 'retried', bool_or(i.delivery_id > {later}),
                                     'pending', bool_or(i.imported_at is null), 'acknowledged',
                                     bool_or(a.acknowledged_at is not null))
            from statement_return_inbox i
            left join statement_return_acknowledgements a using (account, return_ref)
            where i.close_ref = {close} group by i.return_ref;
            """,
        )
        sent = {row["ref"] for row in deliveries if not row["pending"] and (row["retried"] or row["acknowledged"])}
        expected_refs = {row["ref"] for row in allocation["earned"]}
        failures = []
        if missing := expected_refs - sent:
            failures.append(self.fail("statement_return_not_imported", returns=sorted(missing)))
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
        for row in earned + genuine:
            actual = by_line.get(row["line"])
            expected = (row["account"], row.get("amount"), None, row["account"], 1) if "amount" in row else (
                row["account"], 0, row["ref"], None, 0
            )
            if not actual or tuple(actual[k] for k in ("account", "charges", "waiver", "billed_account", "items")) != expected:
                failures.append(self.fail("statement_return_waiver_wrong", event=row["line"], decision=actual))
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
        first = failures[0]
        detail = {"failures": [{"reason": f["reason"], **f.get("detail", {})} for f in failures]}
        if observed is not None:
            detail["observed"] = observed
        return {**first, "detail": detail}

    def evaluate(self, solution=None, trace=None, duration=None):
        app = self.app
        try:
            for pod in ("core-0", "billing-0"):
                app.wait_pod_ready(pod, timeout=180)
            app.wait_pod_ready(f"{SUBSCRIBERS}-0", hidden=True, timeout=180)
        except Exception as exc:  # noqa: BLE001
            return self.fail("routine_jobs_failed", error=f"database unavailable: {exc}")

        failures = []

        def run(step, action, failure_reason=None):
            try:
                return action()
            except Exception as exc:  # noqa: BLE001
                failure = (
                    self.fail(failure_reason, step=step, error=str(exc)[:2000])
                    if failure_reason
                    else self.fail_from_exception(exc, step=step)
                )
                failures.append(failure)
                return None

        def check(action, *args):
            for failure in run(action.__name__, lambda: action(*args)) or ():
                if failure not in failures:
                    failures.append(failure)

        def job(step, action):
            return run(step, action, "routine_jobs_failed")

        def wait_feeds():
            disabled = app.customer("postgres", "select subname from pg_subscription where not subenabled;").split()
            with contextlib.suppress(TimeoutError):
                app.wait_caught_up(timeout=240, skip=disabled)

        observed = self.observe()
        print(f"observed before tonight's routine: {json.dumps(observed)}")
        errors_before = run("subscription_errors", self.subscription_errors)
        check(self.check_duplicate_usage)
        check(self.check_documents)
        check(self.precheck_feeds)
        try:
            job("Vale returns", lambda: app.customer(VALE, f"alter subscription {VALE} enable;"))
            job("onboarding", self.onboard_new_customer)
            touched = job("fresh corrections", lambda: set(self.fresh_corrections()))
            job("wait for corrections", wait_feeds)
            job("nightly extracts", self.nightly_files)
            job("Pell file loader", lambda: app.load_extract_files("pell", "pell_usage"))
            job("pause ingest", lambda: app.set_rate(0))
            run("drain inbox", lambda: app.wait_inbox_empty(timeout=300), "ingest_unavailable")
            run(
                "ingest rollout",
                lambda: app.command("rollout", "status", "statefulset/ingest", "--timeout=60s", timeout=90),
                "ingest_unavailable",
            )
            run("wait for readers", wait_feeds)
            check(self.wait_ledgers_delivered)
            job("re-rate", lambda: app.run_cronjob("billing-rerate", f"billing-rerate-{int(time.time())}"))
            check(self.wait_ledgers_delivered)
            boundary = run(
                "invoice boundary", lambda: int(app.finance("select coalesce(max(invoice_id), 0) from invoices;"))
            )
            return_boundary = None
            if self.state().get("allocation_returns"):
                return_boundary = run("return delivery boundary", lambda: int(app.core(
                    "select coalesce(max(delivery_id), 0) from statement_return_inbox;", database="support"
                )))
                job("customer return resend", lambda: app.run_cronjob(
                    "statement-return-resend", f"statement-return-resend-{int(time.time())}", customer_side=True
                ))
            job("statement return import", lambda: app.run_cronjob("statement-return-import", f"statement-return-import-{int(time.time())}"))
            job("invoice run", lambda: app.invoice_run([*TENANTS, NEW_CUSTOMER[0]], f"invoice-run-{int(time.time())}"))
            for db in [*CUSTOMER_DATABASES, NEW_CUSTOMER[2]]:
                close = job(f"{db} statement close", lambda: app.close_statement(db))
                if close is not None:
                    failures.extend(run(f"{db} statement", lambda: self.check_statements({db: close}, touched)) or ())
            check(self.check_duplicate_usage)
            check(self.check_documents)
            check(self.check_readers, errors_before)
            check(self.check_ledgers, boundary)
            check(self.check_rates)
            check(self.check_statement_return)
            check(self.check_allocation_returns, return_boundary)
            check(self.check_ingest)
        finally:
            with contextlib.suppress(Exception):
                app.set_rate(60)
        if failures:
            return self.report(failures, observed)
        print("✅ every reader, statement, ledger and charge matches the truth")
        return {"success": True, "detail": {"observed": observed}}
