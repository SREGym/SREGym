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


LOG_LINE = re.compile(r"^(?P<ts>\S+ \S+) \S+ \[(?P<pid>\d+)\] (?P<body>.*)$")
SLOT_NAME = re.compile(r'(?:START_REPLICATION SLOT|for slot) "(?P<slot>[^"]+)"')
#: Reader positions that report progress (a commit the reader has applied) and ones that restart decoding.
PROGRESS = {"confirmed", "write", "flush", "replay"}
RESTART = {"restart"}


def reader_lines(log, since):
    """Core log lines of every walsender connection first seen at or after `since`: slot -> one list per connection."""
    by_pid, slot_of, first = {}, {}, {}
    for line in log.splitlines():
        match = LOG_LINE.match(line)
        if not match:
            continue
        pid = match["pid"]
        first.setdefault(pid, match["ts"])
        by_pid.setdefault(pid, []).append(match["body"])
        if found := SLOT_NAME.search(match["body"]):
            slot_of[pid] = found["slot"]
    lines = {}
    for pid, slot in slot_of.items():
        if first[pid] >= since:
            lines.setdefault(slot, []).append(by_pid[pid])
    return lines


def position_kind(body, index):
    """Whether the index-th position in a reader line reports progress or a decoding restart point."""
    if "reading WAL from" in body:
        return "progress" if index == 0 else "restart"
    if "consistent point at" in body or "starting point at" in body:
        return "restart"
    return "progress"


def position_class(position, marks, xid):
    """What WAL record a reader position sits on, with the reader's own failing transaction named as such."""
    found = {("own_batch" if mark == f"record_of_{xid}" else "other_txn" if mark.startswith("record_of_") else mark)
             for mark in marks.get(position, ())}
    return "+".join(sorted(found)) or "other"


def template(body, marks, xid):
    """A reader line's form: positions replaced by their class, names, publication lists and numbers blanked."""
    body = re.sub(r"publication_names '[^']*'", "publication_names 'P'", body)
    body = LSN.sub(lambda m: f"<{position_class(m.group(0), marks, xid)}>", body)
    return re.sub(r"\d+", "N", re.sub(r'"[^"]*"', '"S"', body))


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
        # Week two's corrections target earlier usage: the complaints are about week-one events that week two
        # moved while the stale senders dropped the change. Those lines were rated before the r5 release, so
        # the rates check and the usage-ownership check stay independent.
        week_one = f"event_id <= {problem.week_one_last_event}"
        for database, tenant in COMPLAINERS.items():
            held = {int(e) for e in app.customer(database, "select event_id from usage_events;").split()}
            period = prior if tenant == "quill" else "true"
            def pool(owner):
                return {int(e) for e in app.core(
                    f"select event_id from usage_events where tenant_id = {sql_literal(owner)} and {period} and {week_one};").split()}
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
            {int(e) for e in app.core(
                f"select event_id from usage_events where tenant_id = 'quill' and {prior} and {week_one};").split()}
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
        before, since = app.core(
            "select pg_current_wal_lsn() || '|' || "
            "to_char(clock_timestamp() at time zone current_setting('log_timezone'), 'YYYY-MM-DD HH24:MI:SS.MS');"
        ).strip().split("|")
        app.core(
            f"select correct_usage_batch({sql_literal(ticket)}, {sql_literal(json.dumps(payload))}::jsonb, "
            f"{sql_literal(problem.staff())});"
        )
        problem.note(ticket, problem.staff(),
                     f"Re-sent the missing records and restored the ones in your copy to your account (batch {ticket}).")
        return {**items, "ticket": ticket, "redate_to": redate_to, "lsn_before": before, "since": since}

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
        return {**batch, **failure, "remedy": REMEDIES[database], "command": command}

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
        problems += self.publisher_evidence(batches)
        if not problems:
            print(f"Complaints sealed: {[(b['ticket'], b['remedy']) for b in batches.values()]}")
        return problems

    def publisher_evidence(self, batches):
        """Nothing core keeps or shows separates a skipped batch from one applied after a local delete.

        While a reader is stuck its walsender reports positions the subscriber fed back, and with live ingest
        those can end a record of the reader's own failing transaction. That holds for both remedies, so the
        rule is a comparison: each kept reader position must be a commit end, a running-transactions restart
        point or a record end of the reader's own batch, and each sampled one a commit end or past every batch.
        No reader line may be debug-level or name a skip, and every stuck reader keeps reconnect lines.
        """
        app = self.app
        problems = []
        level = app.core("select setting || '|' || source from pg_settings where name = 'log_min_messages';").strip()
        if level != "warning|default":
            problems.append(f"core log_min_messages is {level}, not the default")
        since = min(b["since"] for b in batches.values())
        logs = app.command("exec", "core-0", "-c", "postgres", "--", "bash", "-c",
                           "cat /var/lib/postgresql/data/pgdata/log/*.log", timeout=120)
        lines = reader_lines(logs, since)
        sampled = {}
        for row in app.rows(app.core, (
            "select json_build_object('slot', s.slot_name, 'restart', s.restart_lsn::text, "
            "'confirmed', s.confirmed_flush_lsn::text, 'write', r.write_lsn::text, 'flush', r.flush_lsn::text, "
            "'replay', r.replay_lsn::text) from pg_replication_slots s "
            "left join pg_stat_replication r on r.pid = s.active_pid where s.slot_type = 'logical';")):
            slot = row.pop("slot")
            sampled[slot] = {k: v for k, v in row.items() if v}
        positions = {(slot, kind, p) for slot in batches for connection in lines.get(slot, []) for body in connection
                     for index, p in enumerate(LSN.findall(body)) for kind in [position_kind(body, index)]}
        positions |= {(slot, "sampled" if field in PROGRESS else "restart", p)
                      for slot in batches for field, p in sampled.get(slot, {}).items()}
        if not positions:
            return ["core kept no reader position for the complaint readers"]
        start = min((p for _, _, p in positions), key=lsn_value)
        xids = ",".join(str(b["xid"]) for b in batches.values())
        marks = {}
        for row in app.core(
            f"select start_lsn || '|' || end_lsn || '|' || resource_manager || '|' || record_type || '|' || xid "
            # Start one WAL page early so the record ending at the oldest position is read too.
            f"from pg_get_wal_records_info({sql_literal(start)}::pg_lsn - 8192, pg_current_wal_lsn()) where "
            "resource_manager = 'Transaction' and record_type like 'COMMIT%' or "
            f"resource_manager = 'Standby' and record_type = 'RUNNING_XACTS' or xid in ({xids});"
        ).split():
            begin, end, manager, kind, xid = row.split("|")
            if kind.startswith("COMMIT"):
                marks.setdefault(begin, set()).add("commit_start")
                marks.setdefault(end, set()).add("commit_end")
            elif kind == "RUNNING_XACTS":
                marks.setdefault(begin, set()).add("running_xacts")
            if manager != "Transaction" and manager != "Standby":
                marks.setdefault(end, set()).add(f"record_of_{xid}")
        # A streaming reader's fed-back position can be the sender's flushed WAL end after a keepalive.
        latest = max(lsn_value(b["finish"]) for b in batches.values())
        allowed = {"progress": {"commit_end", "own_batch"}, "restart": {"running_xacts"}, "sampled": {"commit_end"}}
        for slot, kind, p in sorted(positions):
            found = set(position_class(p, marks, batches[slot]["xid"]).split("+"))
            if not found & allowed[kind] and not (kind == "sampled" and lsn_value(p) > latest):
                problems.append(f"{slot} {kind} position {p} is on {'+'.join(sorted(found))}")
        # SKIP runs only in the subscriber's apply worker (worker.c maybe_start_skipping_changes), so the
        # publisher has no code path that differs between the remedies. Which generic walsender restart lines a
        # connection prints (a saved snapshot or not, a restart point that is also a commit end) depends on
        # timing, not on the remedy, so connections are not matched line by line. What is checked instead is
        # that the remedy itself leaves nothing on the publisher: no debug-level reader line, no line naming a
        # skip, and reconnect lines kept for every stuck reader whichever remedy cleared it.
        forms = {slot: {tuple(sorted(template(body, marks, batch["xid"]) for body in connection))
                        for connection in lines.get(slot, [])}
                 for slot, batch in batches.items()}
        for slot in batches:
            bodies = [body for connection in lines.get(slot, []) for body in connection]
            if not any("forwarding to" in b or "committing after" in b for b in bodies):
                problems.append(f"core kept no reconnect line for {slot}")
            if named := [b for b in bodies if b.startswith("DEBUG") or re.search(r"\bskip", b, re.I)]:
                problems.append(f"core shows {slot} lines that name the remedy: {named[:2]}")
        self.evidence = {"forms": {s: sorted(map(list, f)) for s, f in forms.items()},
                         "positions": sorted((s, k, p, position_class(p, marks, batches[s]["xid"])) for s, k, p in positions)}
        return problems
