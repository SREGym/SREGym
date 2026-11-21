"""Reference repair: correct every reader through the shared feeds, one reader at a time.

Everything happens on core with the company's own levers: the correction tool,
the publications, the feed logins and slots, the nightly-files job and its
configuration, the files publication, and finance and payments SQL. Customer
copies are read here only to save time: an operator derives the same sets from
core (each correction's before and after image, the rows themselves) and from
the WAL the paused slots still hold (Pell's skipped stretch, between the
position Pell asked for and the one core forwarded it to; Quill's direct
update). The hidden choices (what the customers loaded by hand) are not read:
Pell's moved-in rows are offered alone, and month-end gets a delete of the
voided original W whether it holds W or not.

A reader's net change is sent in one transaction: every publication muted, the
rows rewound silently, only the tenant's corrections publication unmuted, the
change replayed, the declared publications restored. The row filter turns the
replay into exactly the inserts, deletes and updates that reader lacks, and no
other reader (billing included) hears it. A sibling on the same publications is
held off (login refused, walsender ended) and its slot advanced past what it
must not hear, with ingest paused at its batch header. A paused reader's slot is
advanced to a fence and only its net is sent, so a row that went out and came
back during the pause is not deleted and booked again. Month-end stops for good
on its first error, and ending its walsender is such an error, so it is let in
once at the fence (core's log then names the position it applied up to: "<its
position> has been already streamed, forwarding to <fence>"), stays connected,
and its net is computed from that position. A row a customer may
have loaded by hand is offered alone: if the reader refuses it, its slot is
advanced past exactly that transaction and the row is updated in place.
"""

import contextlib
import json
import subprocess
import time
from decimal import Decimal

from sregym.conductor.oracles.apps.metering.corrections_handoff import (
    copy_digest,
    finance_differences,
    ledger_rates,
    rate_differences,
)
from sregym.service.apps.metering.application import extracts_conf, parse_extracts_conf, sql_literal

PELL, QUILL, OSTRAVA = "pell_usage", "quill_usage", "ostrava_usage"
LIVE, MONTH_END, VALE = "larkspur_live", "larkspur_monthend", "vale_usage"


def declared_publications(app):
    conf = json.loads(app.command("get", "configmap", "pubsync-core", "-o", "json"))["data"]["publications.conf"]
    declared = {}
    for line in conf.splitlines():
        if line.strip() and not line.startswith("#"):
            name, _table, _filter, publish = (part.strip() for part in line.split("|"))
            declared[name] = publish.replace(",", ", ")
    return declared


def transaction(declared, tenant, rewind, replay):
    mute = "".join(f"alter publication {name} set (publish = '');\n" for name in declared)
    restore = "".join(f"alter publication {name} set (publish = {sql_literal(p)});\n" for name, p in declared.items())
    return (
        f"begin;\n{mute}{rewind}\nalter publication corr_{tenant} set (publish = 'update, delete');\n"
        f"{replay}\n{restore}commit;\n"
    )


def away_from(tenant):
    return "arden" if tenant != "arden" else "brisk"


def ids(events):
    return ",".join(map(str, sorted(events)))


# ------------------------------------------------------------------ slots
def lsn(app):
    return app.core("select pg_current_wal_lsn();").strip()


def confirmed_past(app, slot, target):
    return (
        app.core(
            f"select coalesce(confirmed_flush_lsn >= {sql_literal(target)}::pg_lsn, false) from pg_replication_slots "
            f"where slot_name = {sql_literal(slot)};"
        ).strip()
        == "t"
    )


def wait_confirmed(app, slot, target, timeout):
    deadline = time.monotonic() + timeout
    while not confirmed_past(app, slot, target):
        if time.monotonic() > deadline:
            return False
        time.sleep(1)
    return True


def hold(app, slot):
    """Refuse the reader's login and end its walsender; its slot keeps everything after."""
    app.core(f"alter role feed_{slot} nologin;")
    app.core(
        f"select pg_terminate_backend(active_pid) from pg_replication_slots where slot_name = {sql_literal(slot)} "
        "and active_pid is not null;"
    )
    app.wait_slot(slot, False)


def release(app, slot):
    app.core(f"alter role feed_{slot} login;")


def advance(app, slot, target):
    app.core(f"select pg_replication_slot_advance({sql_literal(slot)}, {sql_literal(target)}::pg_lsn);")


@contextlib.contextmanager
def ingest_paused(app):
    """Hold ingest at its batch header so nothing else is written to usage_events meanwhile."""
    hold_session = subprocess.Popen(
        [
            "kubectl",
            "-n",
            app.namespace,
            "exec",
            "-i",
            "core-0",
            "-c",
            "postgres",
            "--",
            "psql",
            "-XAtq",
            "-U",
            "postgres",
            "-d",
            "usage",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        text=True,
    )
    try:
        hold_session.stdin.write(
            "set application_name = 'batch-pause';\nbegin;\nlock table ingest_batches in exclusive mode;\n"
        )
        hold_session.stdin.flush()
        deadline = time.monotonic() + 120
        while (
            app.core(
                "select count(*) from pg_locks l join pg_stat_activity a using (pid) where a.application_name = "
                "'batch-pause' and l.relation = 'ingest_batches'::regclass and l.granted;"
            ).strip()
            != "1"
        ):
            if time.monotonic() > deadline:
                raise TimeoutError("ingest pause")
            time.sleep(1)
        yield
    finally:
        hold_session.stdin.write("commit;\n")
        hold_session.stdin.close()
        hold_session.wait(timeout=60)


# ---------------------------------------------------------------- re-sends
def voided_row(app, event):
    """A voided usage row as it was, from its correction record on core."""
    return app.core(
        f"select before from usage_corrections where event_id = {event} and kind = 'void' "
        "order by correction_id desc limit 1;"
    ).strip()


def net_transaction(app, declared, tenant, database, digest, skip=(), deletes=(), inserts=()):
    """One transaction that gives one reader of the tenant's feeds exactly what its copy lacks.

    `deletes` are voided rows sent as deletes whether the reader holds them or not; `inserts` are rows on core
    sent as inserts whatever the reader holds (only a deliberate mistake does that)."""
    missing = sorted({e for e in digest["missing"] if e not in skip} | set(inserts))
    extra = sorted({e for e in digest["extra"] if e not in skip} | set(deletes))
    changed = [e for e in digest["changed"] if e not in skip]
    if not (missing or extra or changed):
        return None
    rewind, replay = [], []
    if missing:
        rewind.append(
            f"update usage_events set tenant_id = {sql_literal(away_from(tenant))} where event_id in ({ids(missing)});"
        )
        replay.append(f"update usage_events set tenant_id = {sql_literal(tenant)} where event_id in ({ids(missing)});")
    if extra:
        owners = dict(
            line.split("|")
            for line in app.core(
                f"select event_id || '|' || tenant_id from usage_events where event_id in ({ids(extra)});"
            ).split()
        )
        for event in extra:
            owner = owners.get(str(event))
            if owner:
                rewind.append(f"update usage_events set tenant_id = {sql_literal(tenant)} where event_id = {event};")
                replay.append(f"update usage_events set tenant_id = {sql_literal(owner)} where event_id = {event};")
            else:
                # Put the voided row back silently under this reader's filter, then delete it audibly.
                row = voided_row(app, event)
                rewind.append(
                    "insert into usage_events (event_id, record_id, tenant_id, metric, quantity, occurred_at, batch_id) "
                    f"select event_id, record_id, {sql_literal(tenant)}, metric, quantity, occurred_at, batch_id "
                    f"from jsonb_populate_record(null::usage_events, {sql_literal(row)}::jsonb);"
                )
                replay.append(f"delete from usage_events where event_id = {event};")
    if changed:
        replay.append(f"update usage_events set quantity = quantity where event_id in ({ids(changed)});")
    app.core(transaction(declared, tenant, "\n".join(rewind), "\n".join(replay)))
    return lsn(app)


def send_net(app, declared, tenant, database, skip=(), wait=True):
    end = net_transaction(app, declared, tenant, database, copy_digest(app, database, tenant), skip)
    if end and wait and not wait_confirmed(app, database, end, 300):
        raise RuntimeError(f"{database} did not accept its re-send")
    return end


def offer_alone(app, declared, tenant, database, event, siblings=()):
    """Offer a row the reader may already hold; if it refuses, skip exactly that and update the row in place."""
    away = sql_literal(away_from(tenant))
    app.core(
        transaction(
            declared,
            tenant,
            f"update usage_events set tenant_id = {away} where event_id = {event};",
            f"update usage_events set tenant_id = {sql_literal(tenant)} where event_id = {event};",
        )
    )
    end = lsn(app)
    if wait_confirmed(app, database, end, 45):
        return "accepted"
    hold(app, database)
    advance(app, database, end)
    release(app, database)
    app.core(
        transaction(declared, tenant, "", f"update usage_events set quantity = quantity where event_id = {event};")
    )
    if not wait_confirmed(app, database, lsn(app), 120):
        raise RuntimeError(f"{database} did not take the update of {event}")
    return "refused"


# ----------------------------------------------------------------- readers
def forwarded_from(app, slot, fence, timeout=60):
    """The position a reader asked to start from, as core logs it when its slot is already past it."""
    deadline = time.monotonic() + timeout
    while True:
        out = app.command(
            "exec",
            "core-0",
            "-c",
            "postgres",
            "--",
            "bash",
            "-c",
            f"grep -h -A1 'has been already streamed, forwarding to {fence}$' "
            "/var/lib/postgresql/data/pgdata/log/*.log || true",
        )
        lines = out.splitlines()
        for line, after in zip(lines, lines[1:], strict=False):
            # The next line names the slot: its START_REPLICATION statement or "starting logical decoding".
            if f'"{slot}"' in after:
                return line.split("LOG:", 1)[1].split()[0]
        if time.monotonic() > deadline:
            raise TimeoutError(f"no forwarding line for {slot} at {fence}")
        time.sleep(2)


def repair_pell(app, declared, state, wrong=()):
    skip = set(state["moved_in"])
    if "pell_gap_ignored" in wrong:
        first, last = state["pell_gap"]
        digest = copy_digest(app, PELL, "pell")
        skip |= {e for e in digest["missing"] if first < e <= last} | {state["pell_gap_reduced"]}
    send_net(app, declared, "pell", PELL, skip=skip)
    outcomes = {event: offer_alone(app, declared, "pell", PELL, event) for event in state["moved_in"]}
    if not wrong:
        # The seal leaves the stale-window changes out of Pell's residual classes: the re-sends must fix them.
        digest = copy_digest(app, PELL, "pell")
        if left := set(state.get("pell_base", ())) & {e for kind in digest.values() for e in kind}:
            raise RuntimeError(f"the net re-send left Pell's base events {sorted(left)} wrong")
    return outcomes


def complaint(state, database):
    return (state.get("complaints") or {}).get("batches", {}).get(database)


def repair_complaints(app, state, wrong=()):
    """Restore each contract owner, through the correction tool; a fix the customer asked for stays."""
    batches = (state.get("complaints") or {}).get("batches", {})
    for batch in batches.values():
        if "keep_all_complaints" not in wrong:
            app.core(
                f"select correct_usage({batch['stale']}, 'move', {sql_literal(batch['owner'])}, "
                f"{sql_literal('restored per contract ' + batch['contract'] + ', not per ' + batch['ticket'])}, 'reference');"
            )
        if batch.get("redate") and "revert_all_complaints" in wrong:
            before = app.core(
                f"select before->>'occurred_at' from usage_corrections where event_id = {batch['redate']} "
                f"and kind = 'redate' and batch = {sql_literal(batch['ticket'])};"
            ).strip()
            app.core(f"select correct_usage({batch['redate']}, 'redate', {sql_literal(before)}, 'reverted', 'reference');")


def skipped_items(state, database, wrong):
    """A reader that skipped its batch lacks every item; assuming it applied leaves them out."""
    batch = complaint(state, database)
    if not batch or batch["remedy"] != "skip" or "assume_applied" not in wrong:
        return set()
    return {*batch["rights"], *([batch["redate"]] if batch.get("redate") else [])}


def delete_insert(app, declared, tenant, database, state, wrong):
    """A deliberate mistake: re-send the batch rows as a delete and an insert, whatever the reader holds."""
    batch = complaint(state, database)
    if batch and "delete_insert" in wrong:
        rows = ids(batch["rights"])
        app.core(transaction(declared, tenant, "", (
            f"update usage_events set tenant_id = {sql_literal(away_from(tenant))} where event_id in ({rows});"
            f"update usage_events set tenant_id = {sql_literal(tenant)} where event_id in ({rows});"
        )))
        wait_confirmed(app, database, lsn(app), 120)


def resend_all(app, declared, state):
    """A deliberate mistake: re-send every batch row to every complaining reader as an insert."""
    for database, batch in (state.get("complaints") or {}).get("batches", {}).items():
        tenant = batch.get("tenant") or {LIVE: "larkspur", QUILL: "quill", OSTRAVA: "ostrava"}[database]
        net_transaction(app, declared, tenant, database, copy_digest(app, database, tenant), inserts=batch["rights"])


def repair_quill(app, declared, state, wrong):
    entering, leaving = (state["quill_period"][key] for key in ("entering", "leaving"))
    into, out = entering["event"], leaving["event"]
    skip = {into, out} | skipped_items(state, QUILL, wrong)
    if "quill_from_corrections" in wrong:
        skip.add(state["q"])
    batch = complaint(state, QUILL)
    fix = batch and batch.get("redate") and batch["redate"] not in skip and "revert_all_complaints" not in wrong
    if fix:
        skip.add(batch["redate"])
    send_net(app, declared, "quill", QUILL, skip=skip)
    if fix:
        # Quill finds an update by its old period; rewind the timestamp silently and replay the fix.
        event = batch["redate"]
        before = app.core(
            f"select before->>'occurred_at' from usage_corrections where event_id = {event} and kind = 'redate' "
            f"and batch = {sql_literal(batch['ticket'])};"
        ).strip()
        app.core(transaction(
            declared, "quill",
            f"update usage_events set occurred_at = {sql_literal(before)} where event_id = {event};",
            f"update usage_events set occurred_at = {sql_literal(batch['redate_to'])} where event_id = {event};",
        ))
    delete_insert(app, declared, "quill", QUILL, state, wrong)

    def redate(case, date):
        return f"update usage_events set occurred_at = {sql_literal(case[date])} where event_id = {case['event']};"

    rewind = redate(entering, "before")
    if "quill_period_updates" in wrong:
        # This uses the regular corrections publication to send each change as an UPDATE.
        # Quill has no old row for the first update and already holds the destination of the second.
        app.core(transaction(declared, "quill", redate(leaving, "before"), redate(leaving, "after")))
        app.core(transaction(declared, "quill", rewind, redate(entering, "after")))
        end = lsn(app)
        if not wait_confirmed(app, QUILL, end, 15):
            print(f"Quill rejected the period update for {into}; skip that transaction so the feed can resume.")
            hold(app, QUILL)
            advance(app, QUILL, end)
            release(app, QUILL)
        return

    rewind += f"update usage_events set tenant_id = 'arden' where event_id = {out};"
    replay = (
        f"update usage_events set tenant_id = 'arden', occurred_at = {sql_literal(entering['after'])} "
        f"where event_id = {into};"
        f"update usage_events set tenant_id = 'quill' where event_id = {out};"
    )
    if "quill_period_rebook" in wrong:
        replay += (
            f"alter publication corr_quill set (publish = '');"
            f"update usage_events set tenant_id = 'quill' where event_id = {into};"
            f"alter publication corr_quill set (publish = 'update, delete');"
            f"update usage_events set tenant_id = 'arden' where event_id = {into};"
            f"update usage_events set tenant_id = 'quill' where event_id = {into};"
        )
    else:
        replay += (
            f"alter publication corr_quill set (publish = '');"
            f"update usage_events set tenant_id = 'quill' where event_id = {into};"
        )
    app.core(transaction(declared, "quill", rewind, replay))
    if not wait_confirmed(app, QUILL, lsn(app), 300):
        raise RuntimeError("Quill did not accept its period repair")


def hidden_head(app):
    """Larkspur rows month-end applied past its slot's position (read from a copy of its slot; for a mistake)."""
    origin = app.subscription_origin(MONTH_END)
    rows = app.core(
        "select pg_copy_logical_replication_slot('larkspur_monthend', 'monthend_review', true, 'test_decoding');\n"
        "select substring(data from 'event_id\\[bigint\\]:([0-9]+)') from "
        f"pg_logical_slot_peek_changes('monthend_review', {sql_literal(origin)}::pg_lsn, null) "
        "where data like 'table public.usage_events: INSERT:%tenant_id[text]:''larkspur''%';"
    ).split()[1:]
    return [int(e) for e in rows]


def repair_larkspur(app, declared, state, wrong=()):
    w = state["w"]
    send_net(app, declared, "larkspur", LIVE)
    delete_insert(app, declared, "larkspur", LIVE, state, wrong)
    if "month_end_as_is" in wrong:
        release(app, MONTH_END)
        return {}
    head = hidden_head(app) if "month_end_head" in wrong else []
    # Live already has everything; hold it off while month-end gets only its net. Month-end is let in once,
    # at an empty fence, and stays connected: ending its walsender again would stop it for good.
    hold(app, LIVE)
    try:
        fence = lsn(app)
        advance(app, MONTH_END, fence)
        release(app, MONTH_END)
        app.wait_slot(MONTH_END, True, timeout=60)
        origin = forwarded_from(app, MONTH_END, fence)
        print(f"month-end asked to start from {origin} (its origin: {app.subscription_origin(MONTH_END)})")
        digest = copy_digest(app, MONTH_END, "larkspur")
        if "keep_w" in wrong:
            end = net_transaction(app, declared, "larkspur", MONTH_END, digest, inserts=[w, *head])
        else:
            end = net_transaction(app, declared, "larkspur", MONTH_END, digest, skip={w}, deletes=[w], inserts=head)
        if end and not wait_confirmed(app, MONTH_END, end, 60 if wrong else 300) and not wrong:
            raise RuntimeError("month-end did not accept its re-send")
        advance(app, LIVE, lsn(app))
    finally:
        release(app, LIVE)
    return {}


def repair_vale(app, declared):
    """Vale is offline: advance its slot to the fence and leave only its net waiting for it."""
    advance(app, VALE, lsn(app))
    send_net(app, declared, "vale", VALE, wait=False)


def recall_files(app, account, through_feed=True):
    """Withdraw the account's ready files. Its copy of the files only hears inserts, filtered on the account, so
    the withdrawal goes through the same publication with the whole row as identity, both restored after."""
    update = (
        f"update extract_files set status = 'withdrawn' where account = {sql_literal(account)} and status = 'ready';\n"
    )
    if through_feed:
        update = (
            "begin;\nalter table extract_files replica identity full;\n"
            f"alter publication files_{account} set (publish = 'insert, update');\n{update}"
            f"alter publication files_{account} set (publish = 'insert');\n"
            "alter table extract_files replica identity default;\ncommit;\n"
        )
    app.core(update, database="support")


def retire_nightly_files(app, account="pell", through_feed=True):
    conf = json.loads(app.command("get", "configmap", "correction-extracts", "-o", "json"))["data"]["extracts.conf"]
    accounts = {a: t for a, t in parse_extracts_conf(conf).items() if a != account}
    app.apply_documents([app.config_map("correction-extracts", {"extracts.conf": extracts_conf(accounts)})])
    recall_files(app, account, through_feed)


def repair_finance(problem, linked=False, wrong=()):
    """Send finance each ledger change it missed, through its own rule; offset what was already given back."""
    app = problem.app
    billing = ledger_rates(app.billing)
    finance = ledger_rates(app.finance)
    changed = {line for line, _account in finance_differences(app)}
    rate = "f.rate"
    if "keep_rates" not in wrong:
        if "fill_legacy_rates" in wrong:
            # Exercise the incorrect repair that stamps a band on legacy snapshots.
            changed.update(line for line, snapshot in billing.items() if finance.get(line) != snapshot)
            rate = "case when split_part(r.rate, ',', 2) = ')' then replace(r.rate, ')', 'committed)') else r.rate end"
        elif rate_changes := rate_differences(billing, finance):
            changed.update(rate_changes)
            rate = f"case when r.line_id in ({ids(rate_changes)}) then r.rate else f.rate end"
    columns = "line_id, account, metric, quantity, amount, occurred_at, rated_at, rate"
    if updates := changed & billing.keys():
        rows = app.billing(
            f"copy (select {columns}::text from ledger_lines where line_id in ({ids(updates)})) to stdout;"
        )
        app.finance(
            f"begin;\ncreate temp table rated as select {columns} from ledger_lines with no data;\n"
            f"copy rated from stdin;\n{rows}\\.\n"
            "update ledger_lines f set account = r.account, metric = r.metric, quantity = r.quantity, "
            f"amount = r.amount, occurred_at = r.occurred_at, rated_at = r.rated_at, rate = {rate} "
            "from rated r where f.line_id = r.line_id;\n"
            f"insert into ledger_lines ({columns}) select {columns} from rated r "
            "where not exists (select 1 from ledger_lines f where f.line_id = r.line_id);\ncommit;"
        )
    if voids := changed & (finance.keys() - billing.keys()):
        rows = app.core(f"select event_id from usage_events where event_id in ({ids(voids)});")
        still_in_use = set(map(int, rows.split()))
        if deletions := voids - still_in_use:
            app.finance(f"delete from ledger_lines where line_id in ({ids(deletions)});")
    handoff = problem.load_state()["handoff_at"]
    offsets = [
        (c["account"], Decimal(str(c["amount"])), f"credit note {c['credit_id']} already refunded this usage")
        for c in app.rows(
            app.finance,
            "select row_to_json(c) from credit_notes c "
            f"where issued_at <= {sql_literal(handoff)}::timestamptz and memo like 'Disputed usage%';",
        )
    ] + [
        (r["account"], Decimal(str(r["amount"])), f"refund {r['refund_id']} already returned this usage", r["line"])
        for r in app.rows(
            lambda q: app.core(q, database="payments"),
            "select json_build_object('refund_id', r.refund_id, 'account', c.account, 'amount', r.amount, "
            "'line', substring(r.memo from 'usage event ([0-9]+)')::bigint) "
            f"from refunds r join captures c using (capture_id) where r.refunded_at <= {sql_literal(handoff)}::timestamptz;",
        )
    ]
    for account, amount, reason, *line in offsets:
        # Offsets are booked on the account: a line-linked one would be counted again by finance's own rule
        # the next time that line changes (billing's daily re-rate updates every line).
        target = line[0] if linked and line and line[0] else None
        app.finance(
            "insert into ledger_adjustments (account, line_id, amount, reason) values "
            f"({sql_literal(account)}, {'null' if target is None else target}, {amount}, {sql_literal(reason)});"
        )


def repair_statement_return(app, returned, wrong):
    if "return_leave_waiver" in wrong:
        return
    missing = returned["missing"]
    if "return_clear_only" in wrong:
        app.finance(
            "update ledger_lines set waived_by_return = null, waiver_reason = null "
            f"where account = 'pell' and line_id = {missing};"
        )
        return
    events = [missing]
    if "return_revoke_both" in wrong:
        events.append(returned["genuine"])
    ref, review = map(sql_literal, (returned["ref"], returned["review"]))
    for event in events:
        app.finance(f"select revoke_statement_waiver('pell', {ref}, {event}, {review});")


def repair_allocation_returns(app, allocation, wrong):
    if not allocation or "allocation_leave" in wrong:
        return
    erroneous = allocation["erroneous"]
    if "allocation_clear_only" in wrong:
        app.finance(
            "update ledger_lines set waived_by_return = null, waiver_reason = null "
            f"where line_id in ({ids(row['line'] for row in erroneous)});"
        )
        return
    revocations = list(erroneous)
    if "allocation_revoke_all" in wrong:
        revocations += allocation["genuine"]
    if "allocation_revoke_moved" in wrong:
        revocations += [row for row in allocation["genuine"] if row["moved"]]
    app.finance("\n".join(
        f"select revoke_statement_waiver({sql_literal(row['account'])}, {sql_literal(row['ref'])}, "
        f"{row['line']}, {sql_literal(row['ticket'])});"
        for row in revocations
    ))


def import_eligible_only(app):
    """Wrong repair: the import job takes only returns filed at the close's eligible revision and holds the rest."""
    data = json.loads(app.command("get", "configmap", "finance-jobs", "-o", "json"))["data"]
    script = data["statement-return-import.sh"]
    held = ("from statement_return_inbox i where imported_at is null and (close_ref is null or batch_revision = "
            "(select eligible_revision from allocation_closes c where c.close_ref = i.close_ref))")
    patched = script.replace("from statement_return_inbox where imported_at is null", held)
    if patched == script:
        raise RuntimeError("statement-return-import.sh no longer has the queue query")
    app.apply_documents([app.config_map("finance-jobs", {**data, "statement-return-import.sh": patched})])


def repair(problem, wrong=()):
    """The reference repair; `wrong` names deliberate mistakes for testing the grader."""
    app = problem.app
    state = problem.load_state()
    if "keep_nightly_files" not in wrong:
        through_feed = "files_core_only" not in wrong
        retire_nightly_files(app, through_feed=through_feed)
        if through_feed:
            app.wait_extract_files("pell", PELL)
    # W' (keyed in by support) stays; the original W is voided. Month-end then needs a delete of W whether its
    # DBA loaded W or not, and live (which never had W) needs nothing.
    keep, drop = (state["w"], state["w2"]) if "keep_w" in wrong else (state["w2"], state["w"])
    app.core(f"select correct_usage({drop}, 'void', null, 'duplicate of usage event {keep}', 'reference');")
    repair_complaints(app, state, wrong)
    app.wait_caught_up(skip=(MONTH_END, VALE), timeout=300)
    declared = declared_publications(app)
    with ingest_paused(app):
        outcomes = repair_pell(app, declared, state, wrong)
        repair_quill(app, declared, state, wrong)
        if "ostrava_alone" not in wrong:
            send_net(app, declared, "ostrava", OSTRAVA, skip=skipped_items(state, OSTRAVA, wrong))
            delete_insert(app, declared, "ostrava", OSTRAVA, state, wrong)
        outcomes.update(repair_larkspur(app, declared, state, wrong))
        if "vale_as_is" not in wrong:
            repair_vale(app, declared)
    if "resend_all" in wrong:
        with ingest_paused(app):
            resend_all(app, declared, state)
    print(f"offered alone: {outcomes} (hand-loaded: Pell {state['pell_loaded']}, month-end W {state['w_loaded']})")
    try:
        app.wait_caught_up(skip=(VALE,), timeout=60 if wrong else 300)
    except TimeoutError:
        if not wrong:  # only a deliberate mistake may leave a reader stopped on a row it already holds
            raise
    app.wait_caught_up(app.billing, timeout=300)
    repair_finance(problem, linked="linked_offset" in wrong, wrong=wrong)
    pell_wrong = (*wrong, "return_revoke_both") if "allocation_revoke_all" in wrong else wrong
    repair_statement_return(app, state["statement_return"], pell_wrong)
    repair_allocation_returns(app, state.get("allocation_returns"), wrong)
    if "revision_filter" in wrong:
        import_eligible_only(app)
