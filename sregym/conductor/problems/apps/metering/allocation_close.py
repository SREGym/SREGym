"""Replay the account allocation and statement filing service after the publisher upgrade."""

import json
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sregym.service.apps.metering.application import ALLOCATION_CUSTOMERS, sql_literal

ACCOUNTS = tuple(ALLOCATION_CUSTOMERS.values())
READERS = tuple(ALLOCATION_CUSTOMERS)
ALLOCATIONS = ("BACD", "CBAD", "DBCA", "ACBD", "ADCB", "ABDC", "CBAD", "ABDC")
FILED_REVISIONS = (5, 3, 1, 4)
LOT_TOTALS = dict(zip("ABCD", (47000, 53000, 40000, 60000)))


def sql_json(rows):
    return sql_literal(json.dumps(rows)) + "::jsonb"


def close_window(start):
    return f"{sql_literal(start.isoformat())}, {sql_literal((start + timedelta(minutes=20)).isoformat())}"


def batch_sql(ref, revision, changes="", seed=()):
    """Commit corrections, the receipt, and an audit row together."""
    key = sql_literal(ref)
    receipts = ", ".join(f"({key}, {revision}, '{account}')" for account in ACCOUNTS)
    readings = ""
    if seed:
        readings = f"""
        with batch as (
            insert into ingest_batches (source, records) values ('allocation review', {len(seed)}) returning batch_id
        )
        insert into usage_events (record_id, tenant_id, occurred_at, quantity, metric, batch_id)
        select r.record, r.account, r.at, r.quantity, 'storage_gb_h', batch_id
        from batch cross join jsonb_to_recordset({sql_json(seed)})
             as r(record text, account text, at timestamptz, quantity numeric);
        """
    return f"""
    select pg_current_wal_insert_lsn() as batch_start \\gset
    begin;
    {readings}
    {changes}
    insert into allocation_receipts values {receipts};
    insert into ops.allocation_batches (close_ref, revision) values ({key}, {revision});
    commit;
    update ops.allocation_batches b set commit_lsn = w.end_lsn
    from pg_get_wal_records_info(:'batch_start', pg_current_wal_flush_lsn()) w
    where b.close_ref = {key} and b.revision = {revision} and w.xid = b.xid
      and w.resource_manager = 'Transaction' and w.record_type = 'COMMIT';
    """


class AllocationReplay:
    def __init__(self, problem):
        self.problem = problem
        self.app = problem.app
        self.anchor = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=7)
        self.windows = {
            f"AR-{self.anchor:%y%m%d}-{i + 1:02d}": self.anchor + timedelta(minutes=30 * i)
            for i in range(25)
        }
        self.incident = list(self.windows)[-1]
        self.noise = [ref for ref in self.windows if ref != self.incident]
        self.events = {}
        self.genuine = []
        self.approvals = []
        self.waiver_approvals = []

    def seed_rows(self, ref, account, tag, total, count=1):
        price = Decimal("0.04")
        return [
            {"record": f"{ref}/{tag}/{n}", "account": account, "at": self.windows[ref].isoformat(),
             "quantity": str(Decimal(total) / count / price)}
            for n in range(count)
        ]

    def record_ids(self):
        rows = self.app.core(
            "select record_id || '|' || event_id from usage_events where record_id like "
            f"{sql_literal(f'AR-{self.anchor:%y%m%d}-%')};"
        )
        self.events.update((key, int(event)) for key, event in (row.rsplit("|", 1) for row in rows.split()))
        self.problem.corrected.update(self.events.values())

    def corrections(self, ref, revision, assignments):
        statements = []
        for tag, account in assignments.items():
            for record, event in self.events.items():
                if not record.startswith(f"{ref}/{tag}/"):
                    continue
                statements.append(
                    f"select correct_usage({event}, 'move', '{account}', 'allocation review', "
                    f"'accounts', {sql_literal(f'{ref}/{revision}')}) from usage_events "
                    f"where event_id = {event} and tenant_id <> '{account}';"
                )
        return "\n".join(statements)

    def apply(self, scripts, *, reconnect=None):
        if reconnect:
            self.app.customer(reconnect, f"alter subscription {reconnect} disable;")
            self.app.wait_slot(reconnect, False)
        self.app.core("\n".join(scripts))
        if reconnect:
            self.app.customer(reconnect, f"alter subscription {reconnect} enable;")
            self.app.wait_slot(reconnect, True)

    def wait_receipts(self, ref, revision):
        deadline = time.monotonic() + 25

        def wait(db):
            query = f"select max(revision) from allocation_receipts where close_ref = {sql_literal(ref)};"
            while self.app.customer(db, query).strip() != str(revision):
                if time.monotonic() >= deadline:
                    raise RuntimeError(f"{db} did not receive {ref}/{revision}")
                time.sleep(0.15)

        with ThreadPoolExecutor(max_workers=len(READERS)) as pool:
            list(pool.map(wait, READERS))

    def add_close(self, ref, eligible, cutoff, approval_times, amendment=None):
        ticket = f"ALLOC-{ref}"
        approvals = ", ".join(
            f"({sql_literal(ref)}, {rev}, {sql_literal(at.isoformat())}, "
            f"{sql_literal(amendment if rev > eligible else ref)})"
            for rev, at in enumerate(approval_times, 1)
        )
        self.approvals.append(f"""
        insert into tickets (ticket_id, opened_at, subject, status)
        values ({sql_literal(ticket)}, {sql_literal((cutoff - timedelta(minutes=15)).isoformat())},
                'Account allocation sign-off', 'closed');
        insert into allocation_closes (close_ref, ticket_id, cutoff, eligible_revision)
        values ({sql_literal(ref)}, {sql_literal(ticket)}, {sql_literal(cutoff.isoformat())}, {eligible});
        insert into allocation_approvals values {approvals};
        """)

    def dispute(self, ref, tag, account, cutoff, crossed, moved):
        line = self.events[f"{ref}/{tag}/0"]
        at = cutoff + timedelta(microseconds=1 if crossed else -1)
        self.waiver_approvals.append(
            "insert into statement_waiver_approvals (account, close_ref, line_id, approved_at, reason) values "
            f"('{account}', {sql_literal(ref)}, {line}, {sql_literal(at.isoformat())}, 'acceptance-test usage');"
        )
        self.genuine.append({"line": line, "account": account, "ref": f"{ref}-{account}", "moved": moved,
                             "ticket": f"ALLOC-{ref}"})

    def return_sql(self, ref, account):
        excluded = [str(row["line"]) for row in self.genuine if row["ref"] == f"{ref}-{account}"]
        return (
            f"select file_allocation_return('{ref}-{account}', '{ref}', {close_window(self.windows[ref])}, "
            f"array[{','.join(excluded)}]::bigint[], :'read_at') \\gset"
        )

    def noise_closes(self):
        # Ordinary closes during the same account review. A buyer temporarily
        # holds a lot before returning it, and some tests are agreed off-bill.
        self.apply(
            (batch_sql(ref, 1, seed=[
                *[row for account in ACCOUNTS for row in self.seed_rows(ref, account, account, 500 + 11 * i)],
                *self.seed_rows(ref, "quill", "test-moved", 14500 + 17 * i),
                *self.seed_rows(ref, "ostrava", "test-local", 14700 + 13 * i),
            ]) for i, ref in enumerate(self.noise)),
            reconnect="pell_usage",
        )
        self.record_ids()
        self.apply(
            (batch_sql(ref, 2, self.corrections(ref, 2, {"larkspur": "quill", "test-moved": "pell"}))
             for ref in self.noise),
            reconnect="quill_usage",
        )
        self.apply(
            (batch_sql(ref, 3, self.corrections(ref, 3, {"larkspur": "larkspur"})) for ref in self.noise),
            reconnect="larkspur_live",
        )
        self.wait_receipts(self.noise[-1], 3)
        for ref in self.noise:
            cutoff = self.windows[ref] + timedelta(hours=24)
            self.add_close(ref, 3, cutoff, [cutoff - timedelta(minutes=n) for n in (5, 3, 1)])
            self.dispute(ref, "test-moved", "pell", cutoff, crossed=False, moved=True)
            self.dispute(ref, "test-local", "ostrava", cutoff, crossed=True, moved=False)

        def file(db):
            account = ALLOCATION_CUSTOMERS[db]
            self.app.customer(db, "\n".join(
                "begin isolation level repeatable read;\n"
                "select clock_timestamp() as read_at \\gset\n"
                + self.return_sql(ref, account) + "\ncommit;"
                for ref in self.noise
            ))

        with ThreadPoolExecutor(max_workers=len(READERS)) as pool:
            list(pool.map(file, READERS))
        self.settle(self.noise, 3)

    def incident_close(self):
        ref = self.incident
        approval_times = []
        with ExitStack() as sessions:
            opened = {}
            for revision, allocation in enumerate(ALLOCATIONS[:6], 1):
                approval_times.append(datetime.now(UTC))
                owners = dict(zip(allocation, ACCOUNTS))
                if revision == 1:
                    self.app.core(batch_sql(ref, revision, seed=[
                        *[row for tag, total in LOT_TOTALS.items()
                          for row in self.seed_rows(ref, owners[tag], tag, total, count=4)],
                        *self.seed_rows(ref, "quill", "test-moved", 14650),
                        *self.seed_rows(ref, "quill", "test-local", 14850),
                    ]))
                    self.record_ids()
                else:
                    if revision == 2:
                        owners["test-moved"] = "pell"
                    self.apply([batch_sql(ref, revision, self.corrections(ref, revision, owners))],
                               reconnect="ostrava_usage" if revision == 2 else None)
                self.wait_receipts(ref, revision)
                for db, want in zip(READERS, FILED_REVISIONS):
                    if want == revision:
                        execute = sessions.enter_context(self.app.customer_session(db))
                        execute("begin isolation level repeatable read;\n"
                                f"select clock_timestamp() as read_at from allocation_receipts "
                                f"where close_ref = '{ref}' and revision = {revision} \\gset")
                        opened[db] = execute

            cutoff = datetime.now(UTC)
            self.dispute(ref, "test-moved", "pell", cutoff, crossed=False, moved=True)
            self.dispute(ref, "test-local", "quill", cutoff, crossed=True, moved=False)
            self.settle([ref], 6)

            approval_times.append(datetime.now(UTC))
            self.apply([batch_sql(ref, 7, self.corrections(ref, 7, dict(zip(ALLOCATIONS[6], ACCOUNTS))))])
            self.wait_receipts(ref, 7)
            for db, execute in opened.items():
                execute(self.return_sql(ref, ALLOCATION_CUSTOMERS[db]) + "\ncommit;")

        self.add_close(ref, 6, cutoff, approval_times, amendment=f"{ref}-next")
        # The preview used the same usage IDs. Post the period under the approved
        # allocation before opening the ledger for this close.
        self.apply([batch_sql(ref, 8, self.corrections(ref, 8, dict(zip(ALLOCATIONS[7], ACCOUNTS))))])
        self.wait_receipts(ref, 8)

    def settle(self, refs, revision):
        windows = ", ".join(f"('{ref}', {close_window(self.windows[ref])})" for ref in refs)

        def acknowledgement(db):
            self.app.customer(db, f"""
                insert into allocation_settlements (close_ref, revision, line_count, cents)
                select w.ref, {revision}, p.n, p.cents
                from (values {windows}) as w(ref, start_at, end_at)
                cross join lateral (
                    select count(*) as n, coalesce(sum(quantity) * 4, 0)::bigint as cents
                    from usage_events where occurred_at >= w.start_at::timestamptz
                                      and occurred_at < w.end_at::timestamptz
                ) p;
            """)

        with ThreadPoolExecutor(max_workers=len(READERS)) as pool:
            list(pool.map(acknowledgement, READERS))

    def replay(self):
        self.noise_closes()
        self.incident_close()
        self.app.core("\n".join(["begin;", *self.approvals, *self.waiver_approvals, "commit;"]), database="support")
        self.problem.wait_for_finance()

        with ThreadPoolExecutor(max_workers=len(READERS)) as pool:
            list(pool.map(lambda db: self.app.customer(db, "select send_allocation_returns();"), READERS))
        self.app.replay_job("statement-return-import.sh", "finance")
        self.app.core(
            "insert into statement_return_acknowledgements (account, return_ref) "
            "select account, return_ref from statement_return_inbox where imported_at is not null "
            f"and close_ref <> '{self.incident}' on conflict do nothing;",
            database="support",
        )
        earned = [
            {"line": event, "account": account, "ref": f"{self.incident}-{account}",
             "ticket": f"ALLOC-{self.incident}", "amount": LOT_TOTALS[tag] // 4}
            for tag, account in zip(ALLOCATIONS[5], ACCOUNTS)
            for record, event in self.events.items()
            if record.startswith(f"{self.incident}/{tag}/")
        ]
        erroneous = [row for row in earned if row["account"] in ("larkspur", "ostrava")]
        return {
            "close": self.incident, "closes": len(self.windows), "earned": earned, "erroneous": erroneous,
            "genuine": self.genuine,
        }


def seal_allocations(app, state, earlier_return):
    problems = []
    ref = sql_literal(state["close"])
    earned = {row["line"]: row for row in state["earned"]}
    expected_waivers = {row["line"]: row["ref"] for row in state["genuine"] + state["erroneous"]}
    ledger = app.rows(app.finance, """
        select json_build_object('line', line_id, 'account', account, 'amount', amount,
                                 'waiver', waived_by_return, 'open', invoice_id is null)
        from ledger_lines where waived_by_return is not null or line_id in (
        """ + ",".join(map(str, earned)) + ");")
    by_line = {row["line"]: row for row in ledger}
    actual_waivers = {row["line"]: row["waiver"] for row in ledger
                      if row["waiver"] and row["waiver"] != earlier_return["ref"]}
    if actual_waivers != expected_waivers:
        problems.append(f"allocation waivers differ: {actual_waivers}")
    if any(
        not (actual := by_line.get(line)) or not actual["open"]
        or (actual["account"], actual["amount"]) != (row["account"], row["amount"])
        for line, row in earned.items()
    ):
        problems.append("finance does not have the eligible allocation at its original amounts")
    filed = app.rows(lambda q: app.core(q, database="support"), f"""
        select json_build_object('account', account, 'revision', batch_revision, 'imported', imported_at is not null,
                                 'lines', accepted_rows, 'signed_after_settlement',
                                 signed_at > (select max(settled_at) from allocation_settlements where close_ref = {ref}))
        from statement_return_inbox where close_ref = {ref};
    """)
    returned = [line["event_id"] for file in filed for line in file["lines"]]
    revisions = {file["account"]: file["revision"] for file in filed}
    if (len(returned) != len(earned) or set(returned) != earned.keys()
            or revisions != dict(zip(ACCOUNTS, FILED_REVISIONS))
            or not all(file["imported"] and file["signed_after_settlement"] for file in filed)):
        problems.append(f"four returns did not file the whole allocation from the marked snapshots: {revisions}")
    lots = {row["line"]: tag for tag, account in zip(ALLOCATIONS[5], ACCOUNTS)
            for row in earned.values() if row["account"] == account}
    for file, tag in zip(sorted(filed, key=lambda file: ACCOUNTS.index(file["account"])), "ABCD"):
        if {lots.get(line["event_id"]) for line in file["lines"]} != {tag}:
            problems.append(f"{file['account']} did not return lot {tag}")
    genuine_by_account = {
        row["account"]: by_line[row["line"]]["amount"] for row in state["genuine"]
        if row["ref"].startswith(state["close"]) and row["line"] in by_line
    }
    expected_cents = {
        account: int((LOT_TOTALS[tag] + genuine_by_account.get(account, 0)) * 100)
        for tag, account in zip(ALLOCATIONS[5], ACCOUNTS)
    }
    settlements = app.rows(lambda q: app.core(q, database="support"), f"""
        select row_to_json(s) from allocation_settlements s where close_ref = {ref} and revision = 6;
    """)
    actual_cents = {row["account"]: row["cents"] for row in settlements}
    if actual_cents != expected_cents:
        problems.append(f"settlement acknowledgements did not reflect the eligible allocation: {actual_cents}")
    if app.core("select count(*) from ops.allocation_batches where commit_lsn is null;").strip() != "0":
        problems.append("some allocation receipts have no WAL commit position")
    errors = {row["line"] for row in state["erroneous"]}
    mixed = sum(by_line[line]["amount"] for line in errors if line in by_line and line in actual_waivers)
    prior = by_line.get(earlier_return["missing"], {}).get("amount", 0)
    share = mixed / (mixed + prior) if mixed else 0
    if mixed != 100_000 or share < 0.8:
        problems.append(f"mixed-cut dollar check: {mixed} of {mixed + prior}")
    result = {
        "close_count": state["closes"], "filed_revisions": revisions, "eligible_revision": 6,
        "earned_lot_dollars": sum(row["amount"] for row in earned.values()),
        "mixed_waiver_dollars": mixed, "earlier_waiver_dollars": prior, "error_share": share,
        "genuine_waivers": len(state["genuine"]), "settlement_cents": actual_cents,
        "waived_by_account": {
            account: sum(by_line[line]["amount"] for line in errors if by_line.get(line, {}).get("account") == account)
            for account in ("larkspur", "ostrava")
        },
    }
    print(f"Allocation returns sealed: {json.dumps(result)}")
    return problems, result
