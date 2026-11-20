"""Customer complaints from stale copies, support's re-sends, and the customers' own conflict remedies.

While three customer feeds dropped updates and deletes on 17.4, support made
ordinary corrections: usage moved into those customers, and usage moved out of
them under signed contract amendments. After the 17.6 patch the three customers
compare their copies with the provider's usage report and file tickets. Support
trusts the copies. For each ticket it re-sends the rows in one audited batch by
parking each row on its holding account and moving it back (an UPDATE of a row
the reader lacks is silently skipped, so a plain touch would not reach it). The
batch also restores the rows the customer still holds, although their earlier
move-out was right, and, for Quill, a timestamp fix the customer asked for.

Each restored row is an INSERT of a row its reader already holds, so each feed
stops and retries. The customers clear it with the two remedies PostgreSQL
documents. Ostrava and Quill skip the failed remote transaction (the whole
batch); Larkspur deletes its local row and lets the batch apply. Their tickets
carry their own words and the error context from their logs.
"""

import json
import re
import time

from sregym.service.apps.metering.application import HOLDING, TENANTS, sql_literal


class WorldDidNotTake(RuntimeError):
    """Raised as the problem's own WorldDidNotTake (set by the problem module)."""

COMPLAINERS = {"larkspur_live": "larkspur", "quill_usage": "quill", "ostrava_usage": "ostrava"}
#: How each customer's DBA cleared its stopped feed.
REMEDIES = {"larkspur_live": "local_delete", "quill_usage": "skip", "ostrava_usage": "skip"}
#: Contract amendments that moved usage out of each complainer while its feed dropped deletes.
CONTRACT_OWNERS = {"larkspur": "garnet", "quill": "hollis", "ostrava": "vale"}
DONORS = ("arden", "brisk", "cinder", "dorian", "ember", "fallow", "ionic")
CONTEXT = re.compile(
    r'^(?P<prefix>\S+ \S+ \S+ \[\d+\]) CONTEXT:\s+processing remote data for replication origin "(?P<origin>[^"]+)"'
    r".* in transaction (?P<xid>\d+), finished at (?P<lsn>[0-9A-F]+/[0-9A-F]+)$"
)
LSN = re.compile(r"\b[0-9A-F]{1,8}/[0-9A-F]{1,8}\b")


def lsn_value(text):
    high, low = text.split("/")
    return (int(high, 16) << 32) + int(low, 16)


def failed_transaction(log, origin):
    """The last apply error for one subscription origin: its ERROR, DETAIL and CONTEXT lines, xid and finish LSN."""
    lines = log.splitlines()
    found = None
    for index, line in enumerate(lines):
        match = CONTEXT.match(line)
        if not match or match["origin"] != origin:
            continue
        pid = match["prefix"].rsplit(" ", 1)[1]
        block = [
            entry for entry in lines[max(0, index - 3): index + 1]
            if pid in entry and (" ERROR: " in entry or " DETAIL: " in entry or " CONTEXT: " in entry)
        ]
        found = {"xid": int(match["xid"]), "finish": match["lsn"], "pasted": "\n".join(block)}
    return found


def inside(positions, ranges):
    """Positions strictly inside any (start, finish) range of a skipped transaction."""
    return sorted({p for p in positions for start, finish in ranges if lsn_value(start) < lsn_value(p) < lsn_value(finish)})


def name(tenant):
    return TENANTS[tenant][0]


class ComplaintReplay:
    def __init__(self, problem):
        self.problem = problem
        self.app = problem.app

    # ---------------------------------------------------------- 17.4 window
    def plant(self):
        """Ordinary moves into each complainer and contract moves out of it, while its feed drops updates."""
        problem, app = self.problem, self.app
        prior = f"billing_period < DATE {sql_literal(app.quill_period)}"
        planted = {}
        for database, tenant in COMPLAINERS.items():
            held = {int(e) for e in app.customer(database, "select event_id from usage_events;").split()}
            period = prior if tenant == "quill" else "true"
            def pool(owner):
                return {int(e) for e in app.core(
                    f"select event_id from usage_events where tenant_id = {sql_literal(owner)} and {period};").split()}
            rights = [problem.choose(pool(problem.rng.choice(DONORS)), f"move into {tenant}") for _ in range(2)]
            for event in rights:
                problem.correct(None, "move", tenant, event=event)
            owner = CONTRACT_OWNERS[tenant]
            stale = problem.choose(pool(tenant) & held, f"contract move out of {tenant}")
            record = app.core(f"select record_id from usage_events where event_id = {stale};").strip()
            ticket = problem.ticket(
                tenant,
                f"Contract amendment: usage reassigned to {name(owner)}",
                [(problem.staff(), f"Per the signed amendment, usage of record {record} belongs to {name(owner)}. "
                                   f"Moved usage event {stale} to {name(owner)}.")],
                status="closed",
            )
            app.core(
                f"select correct_usage({stale}, 'move', {sql_literal(owner)}, "
                f"{sql_literal(f'usage reattributed to {name(owner)} per contract')}, "
                f"{sql_literal(problem.staff())}, {sql_literal(ticket)});"
            )
            planted[database] = {"rights": rights, "stale": stale, "owner": owner, "contract": ticket}
        quill_held = {int(e) for e in app.customer("quill_usage", "select event_id from usage_events;").split()}
        planted["quill_usage"]["redate"] = problem.choose(
            {int(e) for e in app.core(f"select event_id from usage_events where tenant_id = 'quill' and {prior};").split()}
            & quill_held, "Quill timestamp fix")
        return planted

    # --------------------------------------------------------- after 17.6
    def subscriber_log(self):
        return self.app.hidden("logs", "subscribers-0", "-c", "postgres", "--since=30m", timeout=120)

    def wait_failure(self, database, origin, timeout=180):
        deadline = time.monotonic() + timeout
        while True:
            found = failed_transaction(self.subscriber_log(), origin)
            if found:
                return found
            if time.monotonic() > deadline:
                raise TimeoutError(f"{database} did not stop on its re-send")
            time.sleep(3)

    def row(self, event):
        rows = self.app.rows(
            self.app.core,
            "select row_to_json(u) from (select tenant_id, metric, quantity, occurred_at from usage_events "
            f"where event_id = {event}) u;",
        )
        return rows[0] if rows else None

    def complain(self, database, items):
        problem, app = self.problem, self.app
        tenant = COMPLAINERS[database]
        held = {int(e) for e in app.customer(database, "select event_id from usage_events;").split()}
        if items["stale"] not in held or held & set(items["rights"]):
            raise WorldDidNotTake(f"{database} received the moves around its complaint")
        def records(events):
            return ", ".join(app.core(f"select record_id from usage_events where event_id = {e};").strip() for e in events)
        words = (
            f"Our usage database and your usage report disagree. Records {records([items['stale']])} are in our "
            f"usage database for our account but missing from your report. Records {records(items['rights'])}, "
            "which your corrections moved to our account, are not in our usage database. Please fix your side so "
            "both match."
        )
        redate_to = None
        if "redate" in items:
            redate_to = app.core(
                f"select least(now() - interval '1 minute', DATE {sql_literal(app.quill_period)} + interval '2 hours' "
                "+ (occurred_at - date_trunc('day', occurred_at)) / 12) "
                f"from usage_events where event_id = {items['redate']};"
            ).strip()
            words += f" Record {records([items['redate']])} was metered at {redate_to[:19]} UTC per our gateway export."
        ticket = problem.ticket(tenant, "Usage copy does not match the usage report", [(problem.customer_name(tenant), words)])
        payload = []
        for event in [*items["rights"], items["stale"]]:
            payload += [
                {"event": event, "kind": "move", "value": HOLDING[0], "reason": f"parked for re-send ({ticket})"},
                {"event": event, "kind": "move", "value": tenant, "reason": f"re-sent to {name(tenant)} per customer usage copy"},
            ]
        if redate_to:
            payload.append({"event": items["redate"], "kind": "redate", "value": redate_to,
                            "reason": "usage timestamp corrected from the customer's gateway export"})
        problem.corrected.update(p["event"] for p in payload)
        before = app.core("select pg_current_wal_lsn();").strip()
        app.core(
            f"select correct_usage_batch({sql_literal(ticket)}, {sql_literal(json.dumps(payload))}::jsonb, "
            f"{sql_literal(problem.staff())});"
        )
        problem.note(ticket, problem.staff(),
                     f"Re-sent the missing records and restored the ones in your copy to your account (batch {ticket}).")
        return {**items, "ticket": ticket, "redate_to": redate_to, "lsn_before": before}

    def remedy(self, database, batch):
        problem, app = self.problem, self.app
        tenant = COMPLAINERS[database]
        origin = app.customer(database, f"select 'pg_' || oid from pg_subscription where subname = {sql_literal(database)};").strip()
        failure = self.wait_failure(database, origin)
        if REMEDIES[database] == "skip":
            command = f"ALTER SUBSCRIPTION {database} SKIP (lsn = '{failure['finish']}');"
            words = ("Our usage subscription stopped after your re-send with the error below. Our DBA skipped the row "
                     "that failed and the feed is running again.")
        else:
            held = app.rows(lambda q: app.customer(database, q),
                            f"select json_build_object('line', line_id) from usage_events where event_id = {batch['stale']};")
            batch["old_line"] = held[0]["line"]
            command = f"DELETE FROM usage_events WHERE event_id = {batch['stale']};"
            words = ("Our live usage subscription stopped on a duplicate after your re-send. We removed the duplicate "
                     "on our side and it caught up.")
        app.customer(database, command)
        problem.note(batch["ticket"], problem.customer_name(tenant),
                     f"{words}\n\n{failure['pasted']}\n\nWhat we ran: {command}")
        problem.note(batch["ticket"], problem.staff(), "Thanks, glad it is running again.")
        start = app.core(
            f"select min(start_lsn) from pg_get_wal_records_info({sql_literal(batch['lsn_before'])}::pg_lsn, "
            f"{sql_literal(failure['finish'])}::pg_lsn) where xid = {failure['xid']};"
        ).strip()
        return {**batch, **failure, "start": start, "remedy": REMEDIES[database], "command": command}

    def run(self, planted):
        app = self.app
        batches = {db: self.complain(db, items) for db, items in planted.items()}
        done = {db: self.remedy(db, batch) for db, batch in batches.items()}
        app.wait_caught_up(slots=list(COMPLAINERS))
        for database, batch in done.items():
            if batch["remedy"] == "skip":
                deadline = time.monotonic() + 120
                while app.customer(database, f"select subskiplsn from pg_subscription where subname = {sql_literal(database)};").strip() != "0/0":
                    if time.monotonic() > deadline:
                        raise TimeoutError(f"{database} kept its skip position")
                    time.sleep(2)
        app.close_statement("larkspur_live")
        truth = {}
        for batch in done.values():
            for event in batch["rights"]:
                truth[event] = self.row(event)
            stale = self.row(batch["stale"])
            truth[batch["stale"]] = {**stale, "tenant_id": batch["owner"]}
            if batch.get("redate"):
                truth[batch["redate"]] = self.row(batch["redate"])
        return {"batches": done, "truth": {str(k): v for k, v in truth.items()}}

    # ---------------------------------------------------------------- seal
    def seal(self, complaints, pitfalls):
        app = self.app
        problems = []
        batches, truth = complaints["batches"], complaints["truth"]
        touched = {int(e) for e in truth}
        if overlap := touched & set(pitfalls):
            problems.append(f"complaint batches touch sealed pitfall rows {sorted(overlap)}")
        for database, batch in batches.items():
            tenant = COMPLAINERS[database]
            held = {int(e) for e in app.customer(database, "select event_id from usage_events;").split()}
            owners = dict(line.split("|") for line in app.core(
                "select event_id || '|' || tenant_id from usage_events where event_id in "
                f"({','.join(map(str, [*batch['rights'], batch['stale']]))});").split())
            if any(owners.get(str(e)) != tenant for e in [*batch["rights"], batch["stale"]]):
                problems.append(f"core does not hold every {batch['ticket']} row at {tenant}: {owners}")
            if batch["stale"] not in held:
                problems.append(f"{database} does not hold the restored row {batch['stale']}")
            applied = set(batch["rights"]) <= held
            if applied != (batch["remedy"] == "local_delete"):
                problems.append(f"{database} applied {batch['ticket']}: {applied}, remedy {batch['remedy']}")
            if batch["remedy"] == "local_delete":
                lines = app.customer(database, f"select line_id || '|' || kind from statement_lines where event_id = {batch['stale']} order by line_id, kind;").split()
                new = app.customer(database, f"select line_id from usage_events where event_id = {batch['stale']};").strip()
                if f"{batch['old_line']}|reversal" not in lines or f"{new}|line" not in lines or str(batch["old_line"]) == new:
                    problems.append(f"{database} did not reverse and rebook the restored row: {lines}")
            if batch.get("redate"):
                period = app.customer(database, f"select billing_period from usage_events where event_id = {batch['redate']};").strip()
                core_period = app.core(f"select billing_period from usage_events where event_id = {batch['redate']};").strip()
                if not period or period == core_period:
                    problems.append(f"{database} took the timestamp fix of {batch['redate']}: {period} vs {core_period}")
        vale = batches["ostrava_usage"]["stale"]
        if app.customer("vale_usage", f"select count(*) from usage_events where event_id = {vale};").strip() != "1":
            problems.append(f"vale_usage does not hold its contract row {vale}")
        ranges = [(b["start"], b["finish"]) for b in batches.values() if b["remedy"] == "skip"]
        if not all(start for start, _ in ranges):
            problems.append(f"no start position for a skipped transaction: {ranges}")
        else:
            logs = app.command("exec", "core-0", "-c", "postgres", "--", "bash", "-c",
                               "cat /var/lib/postgresql/data/pgdata/log/*.log", timeout=120)
            sampled = app.core(
                "select concat_ws(' ', restart_lsn, confirmed_flush_lsn) from pg_replication_slots "
                "union all select concat_ws(' ', sent_lsn, write_lsn, flush_lsn, replay_lsn) from pg_stat_replication;"
            )
            for source, text in (("core log", logs), ("slot or sender positions", sampled)):
                if hits := inside(LSN.findall(text), ranges):
                    problems.append(f"{source} records positions inside a skipped transaction: {hits[:5]}")
        if not problems:
            print(f"Complaints sealed: {[(b['ticket'], b['remedy']) for b in batches.values()]}")
        return problems
