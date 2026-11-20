"""End-state grading for the usage corrections handoff.

After the agent finishes, the company's and its customers' routine work runs as
it would tonight: Vale's site comes back, pubsync onboards a new customer from
the declared configuration, support makes fresh corrections, the nightly
correction files go out if they still do and Pell's loader applies every ready
file in its own copy of the files, traffic settles, billing's daily re-rate
runs, finance's invoice run closes every account and every customer closes its
statement. Then every reader is compared with the truth on
core: each customer copy and statement, billing's ledger, finance's ledger
under its own closed-period rule, the documents already issued, and what each
customer was charged in total.

Checks that tonight's routine cannot change are read first; when one of them
fails the routine is not run, so an unrepaired world stays repairable.
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
    SUBSCRIBERS,
    TENANTS,
    parse_extracts_conf,
    sql_literal,
)

COLUMNS = "record_id, tenant_id, metric, quantity::text, occurred_at::text, batch_id"
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
    """(line, account) -> what finance will bill for it under its own rule: the line plus the rule's adjustments."""
    effective = defaultdict(Decimal)
    for line, (account, amount) in ledger(app.finance).items():
        effective[(line, account)] += amount
    rows = app.finance(
        f"select line_id || '|' || account || '|' || amount from ledger_adjustments where line_id is not null and {RULE_SQL};"
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
    #: Read the checks tonight's routine cannot change first, and stop there if one fails.
    precheck = True
    FAILURE_CLASSES = {
        "usage_entered_twice": "agent",
        "duplicate_usage_missing": "agent",
        "duplicate_usage_changed": "agent",
        "documents_changed": "agent",
        "customer_copy_wrong": "agent",
        "statement_reversed": "agent",
        "statement_rebooked": "agent",
        "statement_lines_wrong": "agent",
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
        return problem.corrections(plan, pause=(0.5, 1.5))

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

    def prechecks(self):
        failures = self.check_duplicate_usage() + self.check_documents()
        lagging = self.lagging_feeds([db for db in CUSTOMER_DATABASES if db != VALE])
        if lagging:
            failures.append(self.fail("subscription_not_streaming", feeds=lagging))
        return failures

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
        disabled = app.customer(
            "postgres", "select string_agg(subname, ' ') from pg_subscription where not subenabled;"
        )
        lagging = sorted(set(self.lagging_feeds(list(databases))) | set(disabled.split()))
        if lagging:
            failures.append(self.fail("subscription_not_streaming", feeds=lagging))
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
            reversed_ = {
                int(e)
                for e in app.customer(
                    database, f"select event_id from statement_lines where kind = 'reversal' and close_id = {close};"
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
            if database not in CUSTOMER_DATABASES:
                continue
            rows = app.customer(
                database,
                "select s.event_id || '|' || s.line_id || '|' || coalesce(u.line_id::text, '') from handoff.sealed s "
                "left join usage_events u using (event_id) where s.correct;",
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

    def check_ledgers(self):
        app = self.app
        failures = []
        truth = truth_lines(app)
        billing = ledger(app.billing)
        wrong = differences(dict(truth), billing)
        if wrong:
            failures.append(self.fail("billing_ledger_wrong", lines=wrong[:20], count=len(wrong)))
        target = {(line, account): amount for line, (account, amount) in truth.items()}
        billed = defaultdict(Decimal)
        usage_items = defaultdict(int)
        rows = app.finance(
            "select i.line_id || '|' || v.account || '|' || i.kind || '|' || i.amount "
            "from invoice_items i join invoices v using (invoice_id) "
            "left join ledger_adjustments a on i.kind = 'adjustment' and a.adjustment_id = i.ref "
            f"where i.line_id is not null and (i.kind = 'usage' or {RULE_SQL.replace('reason', 'a.reason')});"
        )
        for line, account, kind, amount in (r.split("|") for r in rows.split()):
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
        for account, amount in truth.values():
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
        try:
            self.state()
            observed = self.observe()
            print(f"observed before tonight's routine: {json.dumps(observed)}")
            errors_before = self.subscription_errors()
            if self.precheck:
                failures = self.prechecks()
                if failures:
                    print("tonight's routine not run: the end state already fails checks it cannot change")
                    return self.report(failures, observed)
        except Exception as exc:  # noqa: BLE001
            print(f"❌ grading raised: {exc}")
            return self.fail_from_exception(exc)
        try:
            app.customer(VALE, f"alter subscription {VALE} enable;")
            self.onboard_new_customer()
            touched = set(self.fresh_corrections())
            with contextlib.suppress(TimeoutError):
                app.wait_caught_up(timeout=240)
            self.nightly_files()
            app.load_extract_files("pell", "pell_usage")
        except Exception as exc:  # noqa: BLE001
            print(f"❌ tonight's routine jobs failed: {exc}")
            return self.fail("routine_jobs_failed", error=str(exc)[:2000])
        failures = []
        try:
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
                app.run_cronjob("billing-rerate", f"billing-rerate-{int(time.time())}")
            except Exception as exc:  # noqa: BLE001
                failures.append(self.fail("routine_jobs_failed", error=f"re-rate: {exc}"[:2000]))
            failures += self.wait_ledgers_delivered()
            try:
                app.invoice_run([*TENANTS, NEW_CUSTOMER[0]], f"invoice-run-{int(time.time())}")
            except Exception as exc:  # noqa: BLE001
                failures.append(self.fail("routine_jobs_failed", error=f"invoice run: {exc}"[:2000]))
            closes = {db: app.close_statement(db) for db in [*CUSTOMER_DATABASES, NEW_CUSTOMER[2]]}
            failures = self.check_duplicate_usage() + self.check_documents() + failures
            failures += self.check_readers(errors_before)
            failures += self.check_statements(closes, touched)
            failures += self.check_ledgers()
            failures += self.check_ingest()
        except Exception as exc:  # noqa: BLE001
            print(f"❌ grading raised: {exc}")
            return self.fail_from_exception(exc)
        finally:
            with contextlib.suppress(Exception):
                app.set_rate(60)
        if failures:
            return self.report(failures, observed)
        print("✅ every reader, statement, ledger and charge matches the truth")
        return {"success": True, "detail": {"observed": observed}}
