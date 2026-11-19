"""Reference repair: correct every reader through the shared feeds, per reader.

Customer copies are corrected from core only. For each change a copy missed,
one transaction mutes every publication of usage_events, rewinds the event to
what that copy holds, enables only the copy's corrections publication, replays
the change, and restores the declared publications, so the row filter turns the
replay into exactly the delete, insert or update that copy missed and no other
reader (billing included) hears it. A copy that shares a publication with a
database that already holds the row (Larkspur's month-end) is held off for the
replay and its slot advanced past it, with ingest paused at its batch header
so no other change is skipped. Finance's ledger is corrected through its own
rule, and the support credits already paid are offset once.
"""

import json
import subprocess
import time
from decimal import Decimal

from sregym.conductor.oracles.apps.metering.corrections_handoff import copy_digest, finance_differences, ledger
from sregym.service.apps.metering.application import CUSTOMER_DATABASES, sql_literal


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


def row_values(app, database, event):
    return json.loads(
        app.customer(
            database,
            f"select row_to_json(u) from (select event_id, record_id, tenant_id, metric, "
            f"quantity, occurred_at, batch_id from usage_events where event_id = {event}) u;",
        )
    )


def previous_owner(app, event, tenant):
    owner = app.core(
        f"select before->>'tenant_id' from usage_corrections where event_id = {event} and kind = 'move' "
        f"and after->>'tenant_id' = {sql_literal(tenant)} order by correction_id desc limit 1;"
    ).strip()
    return owner or ("arden" if tenant != "arden" else "brisk")


def repair_extra(app, declared, database, tenant, event):
    current = app.core(f"select tenant_id from usage_events where event_id = {event};").strip()
    if current:
        rewind = f"update usage_events set tenant_id = {sql_literal(tenant)} where event_id = {event};"
        replay = f"update usage_events set tenant_id = {sql_literal(current)} where event_id = {event};"
    else:
        row = row_values(app, database, event)
        rewind = (
            "insert into usage_events (event_id, record_id, tenant_id, metric, quantity, occurred_at, batch_id) "
            f"select * from json_populate_record(null::usage_events, {sql_literal(json.dumps(row))});"
        )
        replay = f"delete from usage_events where event_id = {event};"
    app.core(transaction(declared, tenant, rewind, replay))


def repair_changed(app, declared, tenant, event):
    app.core(
        transaction(declared, tenant, "", f"update usage_events set quantity = quantity where event_id = {event};")
    )


def repair_missing(app, declared, tenant, event):
    owner = previous_owner(app, event, tenant)
    app.core(
        transaction(
            declared,
            tenant,
            f"update usage_events set tenant_id = {sql_literal(owner)} where event_id = {event};",
            f"update usage_events set tenant_id = {sql_literal(tenant)} where event_id = {event};",
        )
    )


def wait_for(check, timeout, what):
    deadline = time.monotonic() + timeout
    while not check():
        if time.monotonic() > deadline:
            raise TimeoutError(what)
        time.sleep(1)


def replay_holding(app, declared, tenant, events, held):
    """Replay moved-in rows to one copy while its sibling, which already has them, is held off and skipped past."""
    hold = subprocess.Popen(
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
        hold.stdin.write(
            "set application_name = 'batch-pause';\nbegin;\nlock table ingest_batches in exclusive mode;\n"
        )
        hold.stdin.flush()
        wait_for(
            lambda: (
                app.core(
                    "select count(*) from pg_locks l join pg_stat_activity a using (pid) where a.application_name = 'batch-pause' "
                    "and l.relation = 'ingest_batches'::regclass and l.granted;"
                ).strip()
                == "1"
            ),
            120,
            "ingest pause",
        )
        app.core(f"alter role feed_{held} nologin;")
        target = app.core("select pg_current_wal_lsn();").strip()
        wait_for(
            lambda: (
                app.core(
                    f"select confirmed_flush_lsn >= {sql_literal(target)}::pg_lsn from pg_replication_slots "
                    f"where slot_name = {sql_literal(held)};"
                ).strip()
                == "t"
            ),
            120,
            f"{held} caught up",
        )
        app.core(
            f"select pg_terminate_backend(active_pid) from pg_replication_slots where slot_name = {sql_literal(held)} "
            "and active_pid is not null;"
        )
        wait_for(
            lambda: (
                app.core(f"select active from pg_replication_slots where slot_name = {sql_literal(held)};").strip()
                == "f"
            ),
            60,
            f"{held} released",
        )
        for event in events:
            repair_missing(app, declared, tenant, event)
        app.core(f"select pg_replication_slot_advance({sql_literal(held)}, pg_current_wal_lsn());")
    finally:
        app.core(f"alter role feed_{held} login;")
        hold.stdin.write("commit;\n")
        hold.stdin.close()
        hold.wait(timeout=60)


def repair_customers(app):
    declared = declared_publications(app)
    for database, tenant in CUSTOMER_DATABASES.items():
        digest = copy_digest(app, database, tenant)
        for event in digest["extra"]:
            repair_extra(app, declared, database, tenant, event)
        for event in digest["changed"]:
            repair_changed(app, declared, tenant, event)
        if not digest["missing"]:
            continue
        siblings = [db for db, t in CUSTOMER_DATABASES.items() if t == tenant and db != database]
        holders = {}
        for sibling in siblings:
            present = set(
                app.customer(
                    sibling,
                    f"select event_id from usage_events where event_id in ({','.join(map(str, digest['missing']))});",
                ).split()
            )
            for event in digest["missing"]:
                if str(event) in present:
                    holders.setdefault(sibling, []).append(event)
        held = {e for events in holders.values() for e in events}
        for event in digest["missing"]:
            if event not in held:
                repair_missing(app, declared, tenant, event)
        for sibling, events in holders.items():
            replay_holding(app, declared, tenant, events, sibling)


def repair_finance(problem):
    """Send finance each ledger change it missed, through its own rule; skip lines still on their way."""
    app = problem.app
    present = set(ledger(app.finance))
    for line in sorted({line for line, _account in finance_differences(app)} & present):
        rows = app.rows(app.billing, f"select row_to_json(l) from ledger_lines l where line_id = {line};")
        if rows:
            row = rows[0]
            app.finance(
                f"update ledger_lines set account = {sql_literal(row['account'])}, metric = {sql_literal(row['metric'])}, "
                f"quantity = {row['quantity']}, amount = {row['amount']}, rated_at = {sql_literal(row['rated_at'])} "
                f"where line_id = {line};"
            )
        elif not app.core(f"select 1 from usage_events where event_id = {line};").strip():
            app.finance(f"delete from ledger_lines where line_id = {line};")
    handoff = problem.load_state()["handoff_at"]
    for credit in app.rows(
        app.finance,
        "select row_to_json(c) from credit_notes c "
        f"where issued_at <= {sql_literal(handoff)}::timestamptz and memo like 'Disputed usage%';",
    ):
        app.finance(
            "insert into ledger_adjustments (account, line_id, amount, reason) values "
            f"({sql_literal(credit['account'])}, null, {Decimal(str(credit['amount']))}, "
            f"{sql_literal('credit note ' + str(credit['credit_id']) + ' already refunded this usage')});"
        )


def repair(problem):
    app = problem.app
    repair_customers(app)
    app.wait_caught_up(timeout=300)
    repair_finance(problem)
