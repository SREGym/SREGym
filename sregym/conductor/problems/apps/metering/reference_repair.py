"""Reference repair: correct every reader through the shared feeds, one reader at a time.

Everything happens on core with the company's own levers: the correction tool,
the publications, the feed logins and slots, the nightly-files job and its
configuration, and finance and payments SQL. Customer copies are read here only
to save time; the hidden choices (what the customers loaded by hand) are not
read but found by offering the row alone.

A reader's net change is sent in one transaction: every publication muted, the
rows rewound silently, only the tenant's corrections publication unmuted, the
change replayed, the declared publications restored. The row filter turns the
replay into exactly the inserts, deletes and updates that reader lacks, and no
other reader (billing included) hears it. A sibling on the same publications is
held off (login refused, walsender ended) and its slot advanced past what it
must not hear, with ingest paused at its batch header. A paused reader's slot is
advanced to a fence and only its net is sent, so a row that went out and came
back during the pause is not deleted and booked again. A row a customer may
have loaded by hand is offered alone: if the reader refuses it, its slot is
advanced past exactly that transaction and the row is updated in place.
"""

import contextlib
import json
import subprocess
import time
from decimal import Decimal

from sregym.conductor.oracles.apps.metering.corrections_handoff import copy_digest, finance_differences, ledger
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
def net_transaction(app, declared, tenant, database, digest, skip=()):
    """One transaction that gives one reader of the tenant's feeds exactly what its copy lacks."""
    missing = [e for e in digest["missing"] if e not in skip]
    extra = [e for e in digest["extra"] if e not in skip]
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
                row = json.loads(
                    app.customer(
                        database,
                        "select row_to_json(u) from (select event_id, record_id, tenant_id, metric, quantity, "
                        f"occurred_at, batch_id from usage_events where event_id = {event}) u;",
                    )
                )
                rewind.append(
                    "insert into usage_events (event_id, record_id, tenant_id, metric, quantity, occurred_at, batch_id) "
                    "select event_id, record_id, tenant_id, metric, quantity, occurred_at, batch_id "
                    f"from json_populate_record(null::usage_events, {sql_literal(json.dumps(row))});"
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
def repair_pell(app, declared, state):
    send_net(app, declared, "pell", PELL, skip=set(state["moved_in"]))
    return {event: offer_alone(app, declared, "pell", PELL, event) for event in state["moved_in"]}


def repair_larkspur(app, declared, state, month_end_as_is=False):
    w = state["w"]
    send_net(app, declared, "larkspur", LIVE)
    if month_end_as_is:
        release(app, MONTH_END)
        return {}
    # Live already has everything; hold it off while month-end gets only its net.
    hold(app, LIVE)
    try:
        advance(app, MONTH_END, lsn(app))
        end = net_transaction(app, declared, "larkspur", MONTH_END, copy_digest(app, MONTH_END, "larkspur"), skip={w})
        release(app, MONTH_END)
        if end and not wait_confirmed(app, MONTH_END, end, 300):
            raise RuntimeError("month-end did not accept its re-send")
        outcome = offer_alone(app, declared, "larkspur", MONTH_END, w)
        advance(app, LIVE, lsn(app))
    finally:
        release(app, LIVE)
    return {w: outcome}


def repair_vale(app, declared):
    """Vale is offline: advance its slot to the fence and leave only its net waiting for it."""
    advance(app, VALE, lsn(app))
    send_net(app, declared, "vale", VALE, wait=False)


def retire_nightly_files(app, account="pell"):
    conf = json.loads(app.command("get", "configmap", "correction-extracts", "-o", "json"))["data"]["extracts.conf"]
    accounts = {a: t for a, t in parse_extracts_conf(conf).items() if a != account}
    app.apply_documents([app.config_map("correction-extracts", {"extracts.conf": extracts_conf(accounts)})])
    app.core(
        f"update extract_files set status = 'withdrawn' where account = {sql_literal(account)} and status = 'ready';",
        database="support",
    )


def repair_finance(problem):
    """Send finance each ledger change it missed, through its own rule; offset what was already given back."""
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
    offsets = [
        (c["account"], Decimal(str(c["amount"])), f"credit note {c['credit_id']} already refunded this usage")
        for c in app.rows(
            app.finance,
            "select row_to_json(c) from credit_notes c "
            f"where issued_at <= {sql_literal(handoff)}::timestamptz and memo like 'Disputed usage%';",
        )
    ] + [
        (r["account"], Decimal(str(r["amount"])), f"refund {r['refund_id']} already returned this usage")
        for r in app.rows(
            lambda q: app.core(q, database="payments"),
            "select json_build_object('refund_id', r.refund_id, 'account', c.account, 'amount', r.amount) "
            f"from refunds r join captures c using (capture_id) where r.refunded_at <= {sql_literal(handoff)}::timestamptz;",
        )
    ]
    for account, amount, reason in offsets:
        app.finance(
            "insert into ledger_adjustments (account, line_id, amount, reason) values "
            f"({sql_literal(account)}, null, {amount}, {sql_literal(reason)});"
        )


def repair(problem, wrong=()):
    """The reference repair; `wrong` names deliberate mistakes for testing the grader."""
    app = problem.app
    state = problem.load_state()
    if "keep_nightly_files" not in wrong:
        retire_nightly_files(app)
    app.core(
        f"select correct_usage({state['w2']}, 'void', null, 'entered by hand while the moved-in usage was missing', "
        "'reference');"
    )
    app.wait_caught_up(skip=(MONTH_END, VALE), timeout=300)
    declared = declared_publications(app)
    with ingest_paused(app):
        outcomes = repair_pell(app, declared, state)
        send_net(app, declared, "quill", QUILL)
        send_net(app, declared, "ostrava", OSTRAVA)
        outcomes.update(repair_larkspur(app, declared, state, month_end_as_is="month_end_as_is" in wrong))
        if "vale_as_is" not in wrong:
            repair_vale(app, declared)
    print(f"offered alone: {outcomes} (hand-loaded: Pell {state['pell_loaded']}, month-end W {state['w_loaded']})")
    try:
        app.wait_caught_up(skip=(VALE,), timeout=60 if wrong else 300)
    except TimeoutError:
        if not wrong:  # only a deliberate mistake may leave a reader stopped on a row it already holds
            raise
    app.wait_caught_up(app.billing, timeout=300)
    repair_finance(problem)
