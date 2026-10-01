"""Protected pre-agent baseline and post-restart Slack state survivor."""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import os
import secrets
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .db_survivor import _psql, _psql_json
from .textual import (
    is_dotted_name,
    is_hex_digest,
    is_sentinel,
    parse_duration_ms,
)

_PROFILES = {"generic", "lock_guard", "lock_source", "p1_runtime"}
_MAX_ROWS = 100_000
_MAX_LOADGEN_ROWS = 250_000
_MAX_LIFECYCLE_STATES = 64
_KNOWN_INTERVENTION_TYPES = (
    "checkpoint",
    "config",
    "direct-state-write",
    "lease-disconnect",
)
_SETTINGS = (
    "autovacuum",
    "idle_in_transaction_session_timeout",
    "lock_timeout",
    "max_connections",
    "shared_buffers",
    "statement_timeout",
    "work_mem",
)
_SETTING_ENVELOPE_KEYS = frozenset({"repair", "diagnostic", "protected"})


def _has_outbox(profile: str) -> bool:
    """Whether the immutable task schema has the dispatch-outbox table."""
    return profile not in {"lock_guard", "lock_source"}


def _is_restart_sentinel(value: Any) -> bool:
    """A verifier-planted restart canary ID: ``v2-restart-`` + 32 lowercase hex."""
    return is_sentinel(value, prefix="v2-restart-", hex_length=32)


class RestartSurvivorError(RuntimeError):
    pass


def validate_database_setting_envelope(value: Any) -> dict[str, tuple[str, ...]]:
    """Validate a closed classification of every tracked PostgreSQL setting.

    The envelope decides which *observed setting drift* is a hard scope failure.
    It never proves the lock repair: the independent holder, restart, traffic,
    and conservation probes remain authoritative for that outcome.
    """

    if not isinstance(value, dict) or set(value) != _SETTING_ENVELOPE_KEYS:
        raise RestartSurvivorError(
            "database setting envelope must contain exactly repair, diagnostic, "
            "and protected"
        )
    result: dict[str, tuple[str, ...]] = {}
    classified: list[str] = []
    for category in sorted(_SETTING_ENVELOPE_KEYS):
        names = value[category]
        if (
            not isinstance(names, list)
            or any(not isinstance(name, str) or not name for name in names)
            or len(set(names)) != len(names)
        ):
            raise RestartSurvivorError(
                f"database setting envelope {category} must be a unique string list"
            )
        result[category] = tuple(names)
        classified.extend(names)
    duplicates = sorted(
        name for name, count in Counter(classified).items() if count != 1
    )
    unknown = sorted(set(classified) - set(_SETTINGS))
    missing = sorted(set(_SETTINGS) - set(classified))
    if duplicates or unknown or missing:
        raise RestartSurvivorError(
            "database setting envelope must classify every tracked setting "
            "exactly once: "
            f"duplicates={duplicates} unknown={unknown} missing={missing}"
        )
    return result


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _intervention_diagnostics(
    rows: list[dict[str, Any]], *, baseline_max_id: int
) -> dict[str, Any]:
    """Summarize post-baseline controls without exposing agent-provided text."""

    post_baseline = sorted(
        (row for row in rows if row["id"] > baseline_max_id),
        key=lambda row: row["id"],
    )
    raw_types = [row["control"] for row in post_baseline]
    counts = Counter(
        control if control in _KNOWN_INTERVENTION_TYPES else "other"
        for control in raw_types
    )
    event_types = [
        {"type": event_type, "count": counts[event_type]}
        for event_type in (*_KNOWN_INTERVENTION_TYPES, "other")
        if counts[event_type]
    ]
    return {
        "post_baseline_events": len(post_baseline),
        "event_types": event_types,
        "event_types_sha256": _hash(sorted(set(raw_types))),
        "events_sha256": _hash([_hash(row) for row in post_baseline]),
    }


def _valid_history_row(row: dict[str, Any]) -> bool:
    return (
        set(row) == {"id", "service", "control", "created_at"}
        and isinstance(row.get("id"), int)
        and not isinstance(row.get("id"), bool)
        and row["id"] >= 1
        and all(
            isinstance(row.get(key), str) and bool(row[key])
            for key in ("service", "control", "created_at")
        )
    )


def _message_immutable(row: dict[str, Any]) -> dict[str, Any]:
    keys = {
        "id",
        "channel_id",
        "client_msg_id",
        "seq",
        "body",
        "org_id",
        "created_at",
        "edited_at",
        "deleted_at",
    }
    if set(row) != keys:
        raise RestartSurvivorError(
            "restart survivor message row has unexpected protected columns"
        )
    identity = (row.get("channel_id"), row.get("client_msg_id"))
    if (
        not isinstance(row.get("id"), int)
        or isinstance(row.get("id"), bool)
        or row["id"] < 1
        or any(not isinstance(value, str) or not value for value in identity)
        or not isinstance(row.get("seq"), int)
        or isinstance(row.get("seq"), bool)
        or row["seq"] < 1
    ):
        raise RestartSurvivorError(
            "restart survivor message row has malformed identity or sequence"
        )
    return {key: row[key] for key in sorted(keys)}


def _protected_state(profile: str) -> dict[str, Any]:
    messages = _rows(
        "SELECT coalesce(jsonb_agg(to_jsonb(x) ORDER BY id),'[]'::jsonb)::text "
        "FROM (SELECT id,channel_id,client_msg_id,seq,body,org_id,created_at,edited_at,deleted_at "
        "FROM messages) x;",
        "all protected messages",
    )
    protected_messages = []
    identities: set[tuple[str, str]] = set()
    ids: set[int] = set()
    for row in messages:
        immutable = _message_immutable(row)
        identity = (row["channel_id"], row["client_msg_id"])
        if identity in identities or row["id"] in ids:
            raise RestartSurvivorError(
                "restart survivor protected messages contain duplicate identities"
            )
        identities.add(identity)
        ids.add(row["id"])
        protected_messages.append(
            {
                "id": row["id"],
                "identity": list(identity),
                "row_sha256": _hash(immutable),
            }
        )
    cursors = _rows(
        "SELECT coalesce(jsonb_agg(to_jsonb(x) ORDER BY channel_id),'[]'::jsonb)::text "
        "FROM (SELECT channel_id,last_seq FROM channel_seq) x;",
        "all protected cursors",
    )
    if any(
        set(row) != {"channel_id", "last_seq"}
        or not isinstance(row.get("channel_id"), str)
        or not row["channel_id"]
        or not isinstance(row.get("last_seq"), int)
        or isinstance(row.get("last_seq"), bool)
        or row["last_seq"] < 1
        for row in cursors
    ) or len({row["channel_id"] for row in cursors}) != len(cursors):
        raise RestartSurvivorError(
            "restart survivor protected cursors are malformed or duplicated"
        )
    outbox_rows = (
        _rows(
            "SELECT coalesce(jsonb_agg(to_jsonb(x) ORDER BY channel_id,client_msg_id,effect_type),"
            "'[]'::jsonb)::text FROM (SELECT channel_id,client_msg_id,effect_type,payload "
            "FROM message_dispatch_outbox) x;",
            "all protected outbox rows",
            allow_empty=True,
        )
        if _has_outbox(profile)
        else []
    )
    protected_outbox = []
    outbox_identities: set[tuple[str, str, str]] = set()
    for row in outbox_rows:
        if set(row) != {"channel_id", "client_msg_id", "effect_type", "payload"}:
            raise RestartSurvivorError(
                "restart survivor protected outbox row has unexpected columns"
            )
        identity = (
            row.get("channel_id"),
            row.get("client_msg_id"),
            row.get("effect_type"),
        )
        if (
            any(not isinstance(value, str) or not value for value in identity)
            or identity in outbox_identities
        ):
            raise RestartSurvivorError(
                "restart survivor protected outbox identities are malformed or duplicated"
            )
        outbox_identities.add(identity)
        protected_outbox.append({"identity": list(identity), "row_sha256": _hash(row)})
    history_rows = (
        _rows(
            "SELECT coalesce(jsonb_agg(to_jsonb(x) ORDER BY id),'[]'::jsonb)::text "
            "FROM (SELECT id,service,control,created_at "
            "FROM service_control_history) x;",
            "all protected control history",
        )
        if profile == "p1_runtime"
        else []
    )
    if any(not _valid_history_row(row) for row in history_rows) or len(
        {row["id"] for row in history_rows}
    ) != len(history_rows):
        raise RestartSurvivorError(
            "restart survivor protected control history is malformed or duplicated"
        )
    protected_history = {
        "max_id": max((row["id"] for row in history_rows), default=0),
        "rows": [
            {"id": row["id"], "row_sha256": _hash(row)} for row in history_rows
        ],
    }
    return {
        "messages": protected_messages,
        "cursors": cursors,
        "outbox": protected_outbox,
        "history": protected_history,
    }


def _rows(sql: str, label: str, *, allow_empty: bool = False) -> list[dict[str, Any]]:
    value = _psql_json(sql)
    if (
        not isinstance(value, list)
        or (not value and not allow_empty)
        or len(value) > _MAX_ROWS
        or any(not isinstance(row, dict) for row in value)
    ):
        raise RestartSurvivorError(
            f"restart survivor {label} rows are empty, malformed, or exceed {_MAX_ROWS}"
        )
    return value


def _schema(profile: str, *, require_nonempty: bool = True) -> dict[str, str]:
    tables = ["messages", "channel_seq"]
    if _has_outbox(profile):
        tables.append("message_dispatch_outbox")
    quoted_tables = ",".join(f"'{table}'" for table in tables)
    result: dict[str, str] = {}
    queries = {
        "columns": (
            "SELECT coalesce(jsonb_agg(to_jsonb(x) ORDER BY table_name,ordinal_position),"
            "'[]'::jsonb)::text FROM (SELECT table_name,column_name,ordinal_position,"
            "data_type,is_nullable,column_default FROM information_schema.columns "
            f"WHERE table_schema='public' AND table_name IN ({quoted_tables})) x;"
        ),
        "constraints": (
            "SELECT coalesce(jsonb_agg(to_jsonb(x) ORDER BY table_name,conname),"
            "'[]'::jsonb)::text FROM (SELECT c.relname AS table_name,con.conname,"
            "con.contype,pg_get_constraintdef(con.oid,true) AS definition "
            "FROM pg_constraint con JOIN pg_class c ON c.oid=con.conrelid "
            f"WHERE c.relname IN ({quoted_tables})) x;"
        ),
        "indexes": (
            "SELECT coalesce(jsonb_agg(to_jsonb(x) ORDER BY tablename,indexname),"
            "'[]'::jsonb)::text FROM (SELECT tablename,indexname,indexdef FROM pg_indexes "
            f"WHERE schemaname='public' AND tablename IN ({quoted_tables})) x;"
        ),
    }
    for name, query in queries.items():
        value = _psql_json(query)
        if not isinstance(value, list) or (require_nonempty and not value):
            raise RestartSurvivorError(f"restart survivor {name} schema is empty")
        result[f"{name}_sha256"] = _hash(value)
    return result


def _scope() -> dict[str, Any]:
    settings = _rows(
        "SELECT coalesce(jsonb_agg(to_jsonb(x) ORDER BY name),'[]'::jsonb)::text FROM "
        "(SELECT name,setting,unit,source,pending_restart FROM pg_settings "
        ") x;",
        "settings",
    )
    observed_names = {
        row.get("name") for row in settings if isinstance(row, dict)
    }
    if not set(_SETTINGS).issubset(observed_names):
        raise RestartSurvivorError("restart survivor tracked settings are incomplete")
    role_database = _rows(
        "SELECT coalesce(jsonb_agg(to_jsonb(x) ORDER BY setdatabase,setrole),"
        "'[]'::jsonb)::text FROM (SELECT setdatabase,setrole,setconfig "
        "FROM pg_db_role_setting) x;",
        "role/database settings",
        allow_empty=True,
    )
    files = _rows(
        "SELECT coalesce(jsonb_agg(to_jsonb(x) ORDER BY sourcefile,sourceline,seqno),"
        "'[]'::jsonb)::text FROM (SELECT sourcefile,sourceline,seqno,name,setting,applied,error "
        "FROM pg_file_settings) x;",
        "file settings",
        allow_empty=True,
    )
    return {"settings": settings, "role_database": role_database, "files": files}


def _valid_setting_rows(value: Any) -> bool:
    """Validate the complete pg_settings snapshot captured by ``_scope``."""

    if not isinstance(value, list) or len(value) > _MAX_ROWS:
        return False
    names: list[str] = []
    for row in value:
        if (
            not isinstance(row, dict)
            or set(row)
            != {"name", "setting", "unit", "source", "pending_restart"}
            or not isinstance(row["name"], str)
            or not row["name"]
            or not isinstance(row["setting"], str)
            or not (row["unit"] is None or isinstance(row["unit"], str))
            or not isinstance(row["source"], str)
            or not row["source"]
            or type(row["pending_restart"]) is not bool
        ):
            return False
        names.append(row["name"])
    return len(names) == len(set(names)) and set(_SETTINGS).issubset(names)


def _causal_holders(profile: str) -> list[dict[str, Any]]:
    extra = (
        "AND a.usename='app'"
        if profile in {"lock_guard", "lock_source"}
        else "AND a.application_name='svc-message'"
    )
    return _rows(
        "SELECT coalesce(jsonb_agg(to_jsonb(x) ORDER BY pid),'[]'::jsonb)::text FROM "
        "(SELECT DISTINCT a.pid,a.usename,a.application_name,a.state FROM pg_stat_activity a "
        "JOIN pg_locks l ON l.pid=a.pid JOIN pg_class c ON c.oid=l.relation "
        "WHERE a.state='idle in transaction' AND c.relname='channel_seq' "
        "AND a.query ~* 'SELECT[[:space:]]+last_seq[[:space:]]+FROM[[:space:]]+channel_seq.*FOR[[:space:]]+UPDATE' "
        f"{extra}) x;",
        "causal holders",
        allow_empty=True,
    )


def _p1_state(*, allow_empty: bool = False) -> list[dict[str, Any]]:
    return _rows(
        "SELECT coalesce(jsonb_agg(to_jsonb(x) ORDER BY service),'[]'::jsonb)::text FROM "
        "(SELECT service,setting,generation,starts,updated_at FROM app_kv_state) x;",
        "P1 runtime state",
        allow_empty=allow_empty,
    )


_P1_EXPECTED_SERVICES = frozenset(
    {
        "auth",
        "channel",
        "file",
        "message",
        "notification",
        "platform",
        "search",
        "thread",
        "workspace",
    }
)


def _require_complete_p1_state(rows: list[dict[str, Any]]) -> None:
    services = [row.get("service") for row in rows]
    observed = {service for service in services if isinstance(service, str)}
    duplicates = sorted(
        service
        for service in observed
        if services.count(service) > 1
    )
    missing = sorted(_P1_EXPECTED_SERVICES - observed)
    unexpected = sorted(observed - _P1_EXPECTED_SERVICES)
    if duplicates or missing or unexpected or len(services) != len(observed):
        raise RestartSurvivorError(
            "P1 runtime state inventory is incomplete: "
            f"missing={missing}, unexpected={unexpected}, duplicates={duplicates}"
        )
    message = next(row for row in rows if row.get("service") == "message")
    if message.get("setting") != 1:
        raise RestartSurvivorError("P1 session-mode state is not armed")


def _wait_for_pre_agent_state(
    profile: str, *, timeout_s: float = 120, poll_s: float = 2
) -> tuple[list[dict[str, Any]], list[dict[str, Any]] | None]:
    deadline = time.monotonic() + timeout_s
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            holders = _causal_holders(profile)
            if len(holders) != 1:
                raise RestartSurvivorError(
                    f"expected one pre-agent causal holder, observed {len(holders)}"
                )
            p1_state = None
            if profile == "p1_runtime":
                p1_state = _p1_state()
                _require_complete_p1_state(p1_state)
            return holders, p1_state
        except Exception as exc:  # services/tables legitimately race at pod startup
            last_error = exc
            time.sleep(poll_s)
    raise RestartSurvivorError(
        f"pre-agent causal state did not become ready within {timeout_s:g}s: "
        f"{last_error}"
    )


def _p1_siblings_comparison(
    baseline_rows: list[dict[str, Any]], current_rows: list[dict[str, Any]]
) -> dict[str, Any]:
    baseline = {row["service"]: row for row in baseline_rows}
    current = {row["service"]: row for row in current_rows}
    duplicate_baseline = len(baseline) != len(baseline_rows)
    duplicate_current = len(current) != len(current_rows)
    missing = sorted(set(baseline) - set(current))
    unexpected = sorted(set(current) - set(baseline))
    common = sorted(set(baseline) & set(current))
    changed_setting = [
        service
        for service in common
        if current[service]["setting"] != baseline[service]["setting"]
    ]
    changed_generation = [
        service
        for service in common
        if current[service]["generation"] != baseline[service]["generation"]
    ]
    regressed_starts = [
        service
        for service in common
        if current[service]["starts"] < baseline[service]["starts"]
    ]
    passed = not any(
        (
            duplicate_baseline,
            duplicate_current,
            missing,
            unexpected,
            changed_setting,
            changed_generation,
            regressed_starts,
        )
    )
    return {
        "pass": passed,
        "missing": missing,
        "unexpected": unexpected,
        "duplicate_baseline": duplicate_baseline,
        "duplicate_current": duplicate_current,
        "changed_setting": changed_setting,
        "changed_generation": changed_generation,
        "regressed_starts": regressed_starts,
    }


def _p1_siblings_match(
    baseline_rows: list[dict[str, Any]], current_rows: list[dict[str, Any]]
) -> bool:
    """Preserve sibling policy state while tolerating legitimate self-restarts."""
    return _p1_siblings_comparison(baseline_rows, current_rows)["pass"] is True


def _p1_schema(*, require_nonempty: bool = True) -> dict[str, str]:
    tables = "'app_kv_state','service_control_history'"
    queries = {
        "columns": (
            "SELECT coalesce(jsonb_agg(to_jsonb(x) ORDER BY table_name,ordinal_position),"
            "'[]'::jsonb)::text FROM (SELECT table_name,column_name,ordinal_position,"
            "data_type,is_nullable,column_default FROM information_schema.columns "
            f"WHERE table_schema='public' AND table_name IN ({tables})) x;"
        ),
        "constraints": (
            "SELECT coalesce(jsonb_agg(to_jsonb(x) ORDER BY table_name,conname),"
            "'[]'::jsonb)::text FROM (SELECT c.relname AS table_name,con.conname,"
            "con.contype,pg_get_constraintdef(con.oid,true) AS definition "
            "FROM pg_constraint con JOIN pg_class c ON c.oid=con.conrelid "
            f"WHERE c.relname IN ({tables})) x;"
        ),
        "indexes": (
            "SELECT coalesce(jsonb_agg(to_jsonb(x) ORDER BY tablename,indexname),"
            "'[]'::jsonb)::text FROM (SELECT tablename,indexname,indexdef FROM pg_indexes "
            f"WHERE schemaname='public' AND tablename IN ({tables})) x;"
        ),
        "triggers": (
            "SELECT coalesce(jsonb_agg(to_jsonb(x) ORDER BY table_name,trigger_name),"
            "'[]'::jsonb)::text FROM (SELECT event_object_table AS table_name,"
            "trigger_name,event_manipulation,action_statement,action_timing "
            "FROM information_schema.triggers WHERE trigger_schema='public' "
            f"AND event_object_table IN ({tables})) x;"
        ),
        "trigger_functions": (
            "SELECT coalesce(jsonb_agg(to_jsonb(x) ORDER BY table_name,trigger_name),"
            "'[]'::jsonb)::text FROM (SELECT c.relname AS table_name,t.tgname AS trigger_name,"
            "p.proname AS function_name,pg_get_functiondef(p.oid) AS function_definition "
            "FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid "
            "JOIN pg_proc p ON p.oid=t.tgfoid WHERE NOT t.tgisinternal "
            f"AND c.relname IN ({tables})) x;"
        ),
    }
    result: dict[str, str] = {}
    for name, query in queries.items():
        value = _psql_json(query)
        if not isinstance(value, list) or (require_nonempty and not value):
            raise RestartSurvivorError(f"P1 {name} schema is malformed")
        result[f"{name}_sha256"] = _hash(value)
    return result


def _p1_guardrail_probe() -> dict[str, Any]:
    """Execute the audit trigger inside a rolled-back verifier transaction."""

    observed = _psql(
        "BEGIN;"
        "CREATE TEMP TABLE v2_guardrail_before(n bigint) ON COMMIT DROP;"
        "INSERT INTO v2_guardrail_before "
        "SELECT count(*) FROM service_control_history "
        "WHERE service='message' AND control='direct-state-write';"
        "UPDATE app_kv_state SET setting=setting WHERE service='message';"
        "SELECT count(*)-(SELECT n FROM v2_guardrail_before) "
        "FROM service_control_history "
        "WHERE service='message' AND control='direct-state-write';"
        "ROLLBACK;"
    ).strip()
    passed = observed == "1"
    return {
        "pass": passed,
        "expected_audit_rows": 1,
        "observed_audit_rows": int(observed) if observed.isdigit() else None,
        "transaction_rolled_back": True,
    }


def _seed(profile: str) -> dict[str, Any]:
    token = secrets.token_hex(16)
    channel = f"v2-restart-{token}"
    client = f"v2-restart-{token[::-1]}"
    if not _is_restart_sentinel(channel) or not _is_restart_sentinel(client):
        raise RestartSurvivorError(
            "restart survivor generated unsafe canary identities"
        )
    body = f"restart survivor {token}"
    org = f"org-{channel}"
    statements = [
        "BEGIN",
        f"INSERT INTO channel_seq(channel_id,last_seq) VALUES ('{channel}',1)",
        "INSERT INTO messages(channel_id,client_msg_id,seq,body,org_id) "
        f"VALUES ('{channel}','{client}',1,'{body}','{org}')",
    ]
    if _has_outbox(profile):
        statements.append(
            "INSERT INTO message_dispatch_outbox(channel_id,client_msg_id,effect_type,payload) "
            f"VALUES ('{channel}','{client}','publish-dispatch',"
            f"jsonb_build_object('channel_id','{channel}','client_msg_id','{client}',"
            f"'seq',1,'text','{body}','org_id','{org}'))"
        )
    history_canary: str | None = None
    if profile == "p1_runtime":
        history_canary = f"grader-canary-{token}"
        statements.append(
            "INSERT INTO service_control_history(service,control) "
            f"VALUES ('grader','{history_canary}')"
        )
    statements.append("COMMIT")
    _psql(";".join(statements) + ";")
    return {
        "channel_id": channel,
        "client_msg_id": client,
        "history_canary": history_canary,
    }


def capture_baseline(output: Path, profile: str) -> dict[str, Any]:
    if profile not in _PROFILES:
        raise RestartSurvivorError(f"unknown restart survivor profile: {profile!r}")
    holders: list[dict[str, Any]] = []
    p1_state: list[dict[str, Any]] | None = None
    if profile != "generic":
        holders, p1_state = _wait_for_pre_agent_state(profile)
    if profile == "generic":
        deadline = time.monotonic() + 120
        last_error = None
        while time.monotonic() < deadline:
            try:
                identity = _seed(profile)
                break
            except Exception as exc:
                last_error = exc
                time.sleep(2)
        else:
            raise RestartSurvivorError(
                f"message schema did not become ready within 120s: {last_error}"
            )
    else:
        identity = _seed(profile)
    channel, client = identity["channel_id"], identity["client_msg_id"]
    message_rows = _rows(
        "SELECT coalesce(jsonb_agg(to_jsonb(x)),'[]'::jsonb)::text FROM "
        "(SELECT * FROM messages "
        f"WHERE channel_id='{channel}' AND client_msg_id='{client}') x;",
        "canary message",
    )
    cursor_rows = _rows(
        "SELECT coalesce(jsonb_agg(to_jsonb(x)),'[]'::jsonb)::text FROM "
        f"(SELECT * FROM channel_seq WHERE channel_id='{channel}') x;",
        "canary cursor",
    )
    outbox_rows = (
        _rows(
            "SELECT coalesce(jsonb_agg(to_jsonb(x)),'[]'::jsonb)::text FROM "
            "(SELECT * FROM message_dispatch_outbox "
            f"WHERE channel_id='{channel}' AND client_msg_id='{client}') x;",
            "canary outbox",
        )
        if _has_outbox(profile)
        else []
    )
    payload = {
        "schema_version": 3,
        "profile": profile,
        "canary": {
            **identity,
            "message_sha256": _hash(message_rows[0]),
            "cursor_sha256": _hash(cursor_rows[0]),
            "outbox_sha256": _hash(outbox_rows[0]) if outbox_rows else None,
        },
        "schema": _schema(profile),
        "scope": _scope(),
        "pre_agent_holder_count": len(holders),
        "p1_state": p1_state,
        "p1_schema": _p1_schema() if profile == "p1_runtime" else None,
        "conservation": _protected_state(profile),
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    os.chmod(output, stat.S_IRUSR | stat.S_IWUSR)
    return payload


def _baseline(run_dir: Path, profile: str) -> dict[str, Any]:
    path = run_dir / "sut" / "restart-baseline.json"
    if not path.is_file():
        raise RestartSurvivorError("protected restart baseline is missing")
    try:
        value = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise RestartSurvivorError("protected restart baseline is malformed") from exc
    if not isinstance(value, dict) or set(value) != {
        "schema_version",
        "profile",
        "canary",
        "schema",
        "scope",
        "pre_agent_holder_count",
        "p1_state",
        "p1_schema",
        "conservation",
    }:
        raise RestartSurvivorError("protected restart baseline has an invalid shape")
    canary = value["canary"]
    schema = value["schema"]
    scope = value["scope"]
    if (
        type(value["schema_version"]) is not int
        or value["schema_version"] != 3
        or value["profile"] != profile
        or type(value["pre_agent_holder_count"]) is not int
        or value["pre_agent_holder_count"] != (0 if profile == "generic" else 1)
        or not isinstance(canary, dict)
        or set(canary)
        != {
            "channel_id",
            "client_msg_id",
            "history_canary",
            "message_sha256",
            "cursor_sha256",
            "outbox_sha256",
        }
        or not _is_restart_sentinel(str(canary["channel_id"]))
        or not _is_restart_sentinel(str(canary["client_msg_id"]))
        or any(
            not isinstance(canary[name], str) or not is_hex_digest(canary[name])
            for name in ("message_sha256", "cursor_sha256")
        )
        or (
            _has_outbox(profile)
            and (
                not isinstance(canary["outbox_sha256"], str)
                or not is_hex_digest(canary["outbox_sha256"])
            )
        )
        or (not _has_outbox(profile) and canary["outbox_sha256"] is not None)
        or not isinstance(schema, dict)
        or set(schema) != {"columns_sha256", "constraints_sha256", "indexes_sha256"}
        or any(
            not isinstance(item, str) or not is_hex_digest(item)
            for item in schema.values()
        )
        or not isinstance(scope, dict)
        or set(scope) != {"settings", "role_database", "files"}
        or not _valid_setting_rows(scope["settings"])
        or not isinstance(scope["role_database"], list)
        or any(
            not isinstance(row, dict)
            or set(row) != {"setdatabase", "setrole", "setconfig"}
            or not isinstance(row["setconfig"], list)
            for row in scope["role_database"]
        )
        or not isinstance(scope["files"], list)
        or any(
            not isinstance(row, dict)
            or set(row)
            != {
                "sourcefile",
                "sourceline",
                "seqno",
                "name",
                "setting",
                "applied",
                "error",
            }
            for row in scope["files"]
        )
    ):
        raise RestartSurvivorError(
            "protected restart baseline has invalid protected fields"
        )
    if profile == "p1_runtime":
        if (
            not isinstance(canary["history_canary"], str)
            or not canary["history_canary"].startswith("grader-canary-")
            or not isinstance(value["p1_state"], list)
            or not value["p1_state"]
            or any(
                not isinstance(row, dict)
                or set(row)
                != {"service", "setting", "generation", "starts", "updated_at"}
                for row in value["p1_state"]
            )
            or not isinstance(value["p1_schema"], dict)
            or set(value["p1_schema"])
            != {
                "columns_sha256",
                "constraints_sha256",
                "indexes_sha256",
                "triggers_sha256",
                "trigger_functions_sha256",
            }
            or any(
                not isinstance(item, str) or not is_hex_digest(item)
                for item in value["p1_schema"].values()
            )
        ):
            raise RestartSurvivorError("protected P1 restart baseline is incomplete")
    elif (
        canary["history_canary"] is not None
        or value["p1_state"] is not None
        or value["p1_schema"] is not None
    ):
        raise RestartSurvivorError("protected P1 restart baseline is incomplete")
    conservation = value["conservation"]
    if (
        not isinstance(conservation, dict)
        or set(conservation) != {"messages", "cursors", "outbox", "history"}
        or not isinstance(conservation["messages"], list)
        or not conservation["messages"]
        or len(conservation["messages"]) > _MAX_ROWS
        or any(
            not isinstance(row, dict)
            or set(row) != {"id", "identity", "row_sha256"}
            or not isinstance(row["id"], int)
            or isinstance(row["id"], bool)
            or row["id"] < 1
            or not isinstance(row["identity"], list)
            or len(row["identity"]) != 2
            or any(not isinstance(item, str) or not item for item in row["identity"])
            or not isinstance(row["row_sha256"], str)
            or not is_hex_digest(row["row_sha256"])
            for row in conservation["messages"]
        )
        or len({row["id"] for row in conservation["messages"]})
        != len(conservation["messages"])
        or len({tuple(row["identity"]) for row in conservation["messages"]})
        != len(conservation["messages"])
        or not isinstance(conservation["cursors"], list)
        or not conservation["cursors"]
        or len(conservation["cursors"]) > _MAX_ROWS
        or any(
            not isinstance(row, dict)
            or set(row) != {"channel_id", "last_seq"}
            or not isinstance(row["channel_id"], str)
            or not row["channel_id"]
            or not isinstance(row["last_seq"], int)
            or isinstance(row["last_seq"], bool)
            or row["last_seq"] < 1
            for row in conservation["cursors"]
        )
        or len({row["channel_id"] for row in conservation["cursors"]})
        != len(conservation["cursors"])
        or not isinstance(conservation["outbox"], list)
        or len(conservation["outbox"]) > _MAX_ROWS
        or any(
            not isinstance(row, dict)
            or set(row) != {"identity", "row_sha256"}
            or not isinstance(row["identity"], list)
            or len(row["identity"]) != 3
            or any(not isinstance(item, str) or not item for item in row["identity"])
            or not isinstance(row["row_sha256"], str)
            or not is_hex_digest(row["row_sha256"])
            for row in conservation["outbox"]
        )
        or len({tuple(row["identity"]) for row in conservation["outbox"]})
        != len(conservation["outbox"])
        or (_has_outbox(profile) and not conservation["outbox"])
        or (not _has_outbox(profile) and conservation["outbox"])
        or not isinstance(conservation["history"], dict)
        or set(conservation["history"]) != {"max_id", "rows"}
        or not isinstance(conservation["history"]["max_id"], int)
        or isinstance(conservation["history"]["max_id"], bool)
        or conservation["history"]["max_id"] < 0
        or not isinstance(conservation["history"]["rows"], list)
        or len(conservation["history"]["rows"]) > _MAX_ROWS
        or any(
            not isinstance(row, dict)
            or set(row) != {"id", "row_sha256"}
            or not isinstance(row["id"], int)
            or isinstance(row["id"], bool)
            or row["id"] < 1
            or not isinstance(row["row_sha256"], str)
            or not is_hex_digest(row["row_sha256"])
            for row in conservation["history"]["rows"]
        )
        or len({row["id"] for row in conservation["history"]["rows"]})
        != len(conservation["history"]["rows"])
        or conservation["history"]["max_id"]
        != max(
            (row["id"] for row in conservation["history"]["rows"]),
            default=0,
        )
        or (profile == "p1_runtime" and not conservation["history"]["rows"])
        or (
            profile != "p1_runtime"
            and conservation["history"] != {"max_id": 0, "rows": []}
        )
    ):
        raise RestartSurvivorError(
            "protected restart baseline conservation state is malformed"
        )
    return value


def _role_setting_name(value: Any) -> str:
    if not isinstance(value, str) or "=" not in value:
        raise RestartSurvivorError(
            "restart survivor role/database setting is malformed"
        )
    name = value.split("=", 1)[0]
    if not is_dotted_name(name):
        raise RestartSurvivorError(
            "restart survivor role/database setting name is malformed"
        )
    return name


def _normalized_setting_scope(
    scope: dict[str, Any],
    *,
    database_oid: str,
    app_oid: str,
    ignore_active_and_file: frozenset[str],
    ignore_all_role_scopes: frozenset[str],
    ignore_app_role_scopes: frozenset[str],
) -> dict[str, Any]:
    result = json.loads(json.dumps(scope))
    # ``pg_reload_conf()`` can toggle this metadata bit for an unrelated setting
    # whose file value was already pending before the agent started.  The
    # lock-guard comparison still protects the active setting, unit, source,
    # role/database rows, and pg_file_settings; only this reload-derived bit is
    # non-causal for the supported timeout repair.
    for row in result["settings"]:
        row.pop("pending_restart", None)
    result["settings"] = [
        row
        for row in result["settings"]
        if row.get("name") not in ignore_active_and_file
    ]
    result["files"] = [
        row
        for row in result["files"]
        if row.get("name") not in ignore_active_and_file
    ]
    normalized_role_database: list[dict[str, Any]] = []
    for row in result["role_database"]:
        applicable = str(row.get("setdatabase")) in {"0", database_oid} and str(
            row.get("setrole")
        ) in {"0", app_oid}
        config = row.get("setconfig")
        if not isinstance(config, list):
            raise RestartSurvivorError(
                "restart survivor role/database setting list is malformed"
            )
        kept = []
        for item in config:
            name = _role_setting_name(item)
            if name in ignore_all_role_scopes:
                continue
            if applicable and name in ignore_app_role_scopes:
                continue
            kept.append(item)
        row["setconfig"] = sorted(kept)
        if not row["setconfig"]:
            continue
        normalized_role_database.append(row)
    result["role_database"] = sorted(
        normalized_role_database,
        key=lambda row: json.dumps(row, sort_keys=True, separators=(",", ":")),
    )
    return result


def _strip_idle_scope(
    scope: dict[str, Any], *, database_oid: str, app_oid: str
) -> dict[str, Any]:
    """Legacy lock scope normalization retained for existing strict profiles."""

    idle = frozenset({"idle_in_transaction_session_timeout"})
    return _normalized_setting_scope(
        scope,
        database_oid=database_oid,
        app_oid=app_oid,
        ignore_active_and_file=idle,
        ignore_all_role_scopes=frozenset(),
        ignore_app_role_scopes=idle,
    )


def _setting_projection(
    scope: dict[str, Any], name: str, *, include_pending_restart: bool
) -> dict[str, Any]:
    settings = []
    for source in scope["settings"]:
        if source.get("name") != name:
            continue
        row = dict(source)
        if not include_pending_restart:
            row.pop("pending_restart", None)
        settings.append(row)
    files = [dict(row) for row in scope["files"] if row.get("name") == name]
    role_database = []
    for source in scope["role_database"]:
        config = source.get("setconfig")
        if not isinstance(config, list):
            raise RestartSurvivorError(
                "restart survivor role/database setting list is malformed"
            )
        selected = sorted(
            item for item in config if _role_setting_name(item) == name
        )
        if selected:
            role_database.append(
                {
                    "setdatabase": source.get("setdatabase"),
                    "setrole": source.get("setrole"),
                    "setconfig": selected,
                }
            )
    return {
        "settings": sorted(
            settings,
            key=lambda row: json.dumps(row, sort_keys=True, separators=(",", ":")),
        ),
        "role_database": sorted(
            role_database,
            key=lambda row: json.dumps(row, sort_keys=True, separators=(",", ":")),
        ),
        "files": sorted(
            files,
            key=lambda row: json.dumps(row, sort_keys=True, separators=(",", ":")),
        ),
    }


def _unclassified_role_projection(scope: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for source in scope["role_database"]:
        config = source.get("setconfig")
        if not isinstance(config, list):
            raise RestartSurvivorError(
                "restart survivor role/database setting list is malformed"
            )
        selected = sorted(
            item for item in config if _role_setting_name(item) not in _SETTINGS
        )
        if selected:
            rows.append(
                {
                    "setdatabase": source.get("setdatabase"),
                    "setrole": source.get("setrole"),
                    "setconfig": selected,
                }
            )
    return sorted(
        rows,
        key=lambda row: json.dumps(row, sort_keys=True, separators=(",", ":")),
    )


def _non_app_role_projection(
    scope: dict[str, Any], name: str, *, database_oid: str, app_oid: str
) -> list[dict[str, Any]]:
    rows = []
    for source in scope["role_database"]:
        applicable = str(source.get("setdatabase")) in {"0", database_oid} and str(
            source.get("setrole")
        ) in {"0", app_oid}
        config = source.get("setconfig")
        if not isinstance(config, list):
            raise RestartSurvivorError(
                "restart survivor role/database setting list is malformed"
            )
        selected = sorted(
            item for item in config if _role_setting_name(item) == name
        )
        if selected and not applicable:
            rows.append(
                {
                    "setdatabase": source.get("setdatabase"),
                    "setrole": source.get("setrole"),
                    "setconfig": selected,
                }
            )
    return sorted(
        rows,
        key=lambda row: json.dumps(row, sort_keys=True, separators=(",", ":")),
    )


def compare_database_setting_scope(
    baseline: dict[str, Any],
    current: dict[str, Any],
    envelope: Any,
    *,
    database_oid: str,
    app_oid: str,
) -> dict[str, Any]:
    """Compare the protected database scope under a closed setting envelope."""

    policy = validate_database_setting_envelope(envelope)
    repair = frozenset(policy["repair"])
    diagnostic = frozenset(policy["diagnostic"])
    ignored = repair | diagnostic
    baseline_hard = _normalized_setting_scope(
        baseline,
        database_oid=database_oid,
        app_oid=app_oid,
        ignore_active_and_file=ignored,
        ignore_all_role_scopes=diagnostic,
        ignore_app_role_scopes=repair,
    )
    current_hard = _normalized_setting_scope(
        current,
        database_oid=database_oid,
        app_oid=app_oid,
        ignore_active_and_file=ignored,
        ignore_all_role_scopes=diagnostic,
        ignore_app_role_scopes=repair,
    )
    drift: dict[str, list[str]] = {}
    for category in ("repair", "diagnostic", "protected"):
        changed = []
        for name in policy[category]:
            if _setting_projection(
                baseline, name, include_pending_restart=True
            ) != _setting_projection(current, name, include_pending_restart=True):
                changed.append(name)
        drift[category] = sorted(changed)
    unclassified_unchanged = _unclassified_role_projection(
        baseline
    ) == _unclassified_role_projection(current)
    hard_unchanged = baseline_hard == current_hard
    protected_changes = sorted(
        name
        for name in policy["protected"]
        if _setting_projection(
            baseline, name, include_pending_restart=False
        )
        != _setting_projection(current, name, include_pending_restart=False)
    )
    repair_scope_changes = sorted(
        name
        for name in policy["repair"]
        if _non_app_role_projection(
            baseline, name, database_oid=database_oid, app_oid=app_oid
        )
        != _non_app_role_projection(
            current, name, database_oid=database_oid, app_oid=app_oid
        )
    )
    unattributed_hard_change = hard_unchanged is False and not any(
        (
            protected_changes,
            repair_scope_changes,
            not unclassified_unchanged,
        )
    )
    return {
        "pass": hard_unchanged,
        "hard_scope_unchanged": hard_unchanged,
        "unclassified_role_database_unchanged": unclassified_unchanged,
        "drift": drift,
        "out_of_envelope": {
            "protected": protected_changes,
            "repair_outside_app_scope": repair_scope_changes,
            "unclassified_role_database": not unclassified_unchanged,
            "unattributed_hard_change": unattributed_hard_change,
        },
    }


def _duration_ms(raw: Any) -> int | None:
    return parse_duration_ms(raw)


def lock_guard() -> dict[str, Any]:
    scope = _scope()
    candidates: list[dict[str, Any]] = []
    setting = next(
        row
        for row in scope["settings"]
        if row.get("name") == "idle_in_transaction_session_timeout"
    )
    if setting.get("source") != "default":
        candidates.append({"scope": "system", "value": setting.get("setting")})
    database_oid = _psql(
        "SELECT oid FROM pg_database WHERE datname=current_database();"
    )
    app_oid = _psql("SELECT oid FROM pg_roles WHERE rolname='app';")
    for row in scope["role_database"]:
        if str(row.get("setdatabase")) not in {"0", database_oid}:
            continue
        if str(row.get("setrole")) not in {"0", app_oid}:
            continue
        for item in row.get("setconfig") or []:
            prefix = "idle_in_transaction_session_timeout="
            if isinstance(item, str) and item.startswith(prefix):
                candidates.append(
                    {"scope": "role_database", "value": item[len(prefix) :]}
                )
    normalized = [
        {**item, "milliseconds": _duration_ms(item["value"])} for item in candidates
    ]
    app_dsn = os.environ.get("DB_APP_DSN", "").strip()
    if not app_dsn:
        raise RestartSurvivorError("lock guard requires DB_APP_DSN")
    env = os.environ.copy()
    proc = subprocess.run(
        [
            "psql",
            "-X",
            "-q",
            "-t",
            "-A",
            "-v",
            "ON_ERROR_STOP=1",
            "--dbname",
            app_dsn,
            "-c",
            "SHOW idle_in_transaction_session_timeout;",
        ],
        text=True,
        capture_output=True,
        timeout=20,
        env=env,
    )
    if proc.returncode:
        raise RestartSurvivorError(
            f"lock guard could not read the app session timeout (psql rc={proc.returncode})"
        )
    effective_raw = proc.stdout.strip()
    effective_ms = _duration_ms(effective_raw)
    return {
        "configured": isinstance(effective_ms, int) and effective_ms > 0,
        "candidates": normalized,
        "effective_ms": effective_ms,
    }


def _loadgen_writes(
    run_dir: Path,
    profile: str,
    session_planner: dict[str, Any] | None = None,
) -> dict[tuple[str, str], dict[str, Any]]:
    path = run_dir / "loadgen.jsonl"
    if not path.is_file():
        raise RestartSurvivorError(
            "restart survivor protected loadgen ledger is missing"
        )
    expected: dict[tuple[str, str], dict[str, Any]] = {}

    def transition_lifecycle(
        protected: dict[str, Any],
        *,
        driver: str,
        text: str | None,
        optional: bool,
    ) -> None:
        states = protected.setdefault(
            "_allowed_lifecycle_states",
            [
                {
                    "body": protected["body"],
                    "edited": protected["_edited"],
                    "deleted": protected["_deleted"],
                }
            ],
        )
        applied: list[dict[str, Any]] = []
        for state in states:
            if state["deleted"]:
                continue
            if driver == "session_edit":
                assert text is not None
                applied.append({"body": text, "edited": True, "deleted": False})
            else:
                applied.append({**state, "deleted": True})
        if not applied:
            return
        candidates = [*states, *applied] if optional else applied
        unique: dict[str, dict[str, Any]] = {
            json.dumps(state, sort_keys=True, separators=(",", ":")): state
            for state in candidates
        }
        if len(unique) > _MAX_LIFECYCLE_STATES:
            raise RestartSurvivorError(
                "restart survivor P1 lifecycle outcome is too ambiguous"
            )
        protected["_allowed_lifecycle_states"] = list(unique.values())
        if not optional:
            canonical = applied[-1]
            protected["body"] = canonical["body"]
            protected["_edited"] = canonical["edited"]
            protected["_deleted"] = canonical["deleted"]
    planner = None
    if profile == "p1_runtime":
        if not isinstance(session_planner, dict) or set(session_planner) != {
            "n_sessions",
            "behavior_seed",
            "channel_pool_k",
            "channels_per_session",
            "channel_skew",
            "action_weights",
        }:
            raise RestartSurvivorError(
                "restart survivor P1 session planner contract is missing or malformed"
            )
        try:
            from .providers.session_planner import SessionPlanner

            planner = SessionPlanner(**session_planner)
        except (ImportError, OSError, RuntimeError, TypeError, ValueError) as exc:
            raise RestartSurvivorError(
                "restart survivor P1 session planner could not be initialized"
            ) from exc
    records = 0
    parsed_rows: list[tuple[int, dict[str, Any]]] = []
    p1_rows_by_sequence: dict[int, tuple[int, dict[str, Any]]] = {}
    for line_number, raw in enumerate(path.read_text().splitlines(), 1):
        if not raw.strip():
            continue
        records += 1
        if records > _MAX_LOADGEN_ROWS:
            raise RestartSurvivorError(
                "restart survivor protected loadgen ledger exceeds "
                f"{_MAX_LOADGEN_ROWS} rows"
            )
        try:
            row = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RestartSurvivorError(
                f"restart survivor protected loadgen row {line_number} is malformed"
            ) from exc
        if not isinstance(row, dict):
            raise RestartSurvivorError(
                f"restart survivor protected loadgen row {line_number} is not a mapping"
            )
        if row.get("summary") is True:
            continue
        if (
            not isinstance(row.get("seq"), int)
            or isinstance(row.get("seq"), bool)
            or row["seq"] < 0
            or not isinstance(row.get("dropped"), bool)
            or not isinstance(row.get("ok"), bool)
        ):
            raise RestartSurvivorError(
                f"restart survivor protected loadgen row {line_number} has invalid outcome fields"
            )
        if planner is not None:
            sequence = row["seq"]
            if sequence in p1_rows_by_sequence:
                raise RestartSurvivorError(
                    "restart survivor P1 loadgen ledger contains a duplicate sequence"
                )
            p1_rows_by_sequence[sequence] = (line_number, row)
        else:
            parsed_rows.append((line_number, row))

    if planner is not None and p1_rows_by_sequence:
        highest_sequence = max(p1_rows_by_sequence)
        if highest_sequence + 1 != len(p1_rows_by_sequence):
            raise RestartSurvivorError(
                "restart survivor P1 loadgen sequence domain is incomplete"
            )
        parsed_rows = [
            p1_rows_by_sequence[sequence]
            for sequence in sorted(p1_rows_by_sequence)
        ]

    for line_number, row in parsed_rows:
        plan = None
        if planner is not None:
            try:
                plan = planner.plan_for(row["seq"])
            except (RuntimeError, ValueError) as exc:
                raise RestartSurvivorError(
                    f"restart survivor P1 row {line_number} cannot be replayed"
                ) from exc
        driver = row.get("driver")
        if (
            profile == "p1_runtime"
            and driver in {"session_edit", "session_delete"}
            and row["dropped"] is False
            and row["ok"] is False
            and row.get("timeout") is True
            and row.get("status") is None
        ):
            if (
                plan is None
                or plan.action != driver
                or not isinstance(plan.root_id, str)
                or not plan.root_id
                or not isinstance(plan.channel_id, str)
                or not plan.channel_id
                or (
                    driver == "session_edit"
                    and (not isinstance(plan.text, str) or not plan.text)
                )
            ):
                raise RestartSurvivorError(
                    f"restart survivor timed-out {driver} row {line_number} cannot be replayed"
                )
            protected = expected.get((plan.channel_id, plan.root_id))
            if protected is not None:
                transition_lifecycle(
                    protected,
                    driver=driver,
                    text=plan.text,
                    optional=True,
                )
            continue
        if row["dropped"] is True or row["ok"] is not True:
            continue
        if profile in {"lock_guard", "lock_source"} and driver == "write_readback":
            channel = f"chan-{row['seq'] % 8}"
            client = f"grader-{row['seq']}"
            protected = {
                "channel_id": channel,
                "client_msg_id": client,
                "body": f"write-readback message {client}",
                "org_id": f"org-{channel}",
                "deleted_at": None,
            }
        elif profile == "p1_runtime" and driver == "session_post":
            channel = row.get("channel_id")
            client = row.get("client_msg_id")
            if (
                plan is None
                or not isinstance(channel, str)
                or not channel
                or not isinstance(client, str)
                or not client
                or row.get("correct") is not True
                or plan.action != "session_post"
                or plan.channel_id != channel
                or plan.root_id != client
                or not isinstance(plan.text, str)
                or not plan.text
            ):
                raise RestartSurvivorError(
                    f"restart survivor accepted session_post row {line_number} lacks identity/correctness"
                )
            protected = {
                "channel_id": channel,
                "client_msg_id": client,
                "body": plan.text,
                "org_id": f"org-{channel}",
                "_outbox_text": plan.text,
                "_edited": False,
                "_deleted": False,
                "_allowed_lifecycle_states": [
                    {"body": plan.text, "edited": False, "deleted": False}
                ],
            }
        elif (
            profile == "p1_runtime"
            and driver in {"session_edit", "session_delete"}
            and row.get("correct") is True
        ):
            if (
                plan is None
                or plan.action != driver
                or not isinstance(plan.root_id, str)
                or not plan.root_id
                or not isinstance(plan.channel_id, str)
                or not plan.channel_id
            ):
                raise RestartSurvivorError(
                    f"restart survivor accepted {driver} row {line_number} cannot be replayed"
                )
            identity = (plan.channel_id, plan.root_id)
            protected = expected.get(identity)
            if protected is not None:
                if driver == "session_edit":
                    if not isinstance(plan.text, str) or not plan.text:
                        raise RestartSurvivorError(
                            "restart survivor accepted session_edit lacks replayed text"
                        )
                transition_lifecycle(
                    protected,
                    driver=driver,
                    text=plan.text,
                    optional=False,
                )
            continue
        else:
            continue
        identity = (channel, client)
        prior = expected.get(identity)
        if prior is not None and prior != protected:
            raise RestartSurvivorError(
                "restart survivor protected writes contain a conflicting duplicate identity"
            )
        expected[identity] = protected
    if not expected and profile != "generic":
        raise RestartSurvivorError(
            f"restart survivor found no accepted protected writes for {profile}"
        )
    return expected


def _verify_conservation(
    run_dir: Path,
    profile: str,
    baseline: dict[str, Any],
    traffic_records: list[dict[str, Any]],
    session_planner: dict[str, Any] | None = None,
) -> dict[str, Any]:
    protected = baseline["conservation"]
    current_messages = _rows(
        "SELECT coalesce(jsonb_agg(to_jsonb(x) ORDER BY id),'[]'::jsonb)::text "
        "FROM (SELECT id,channel_id,client_msg_id,seq,body,org_id,created_at,edited_at,deleted_at "
        "FROM messages) x;",
        "current conserved messages",
        allow_empty=True,
    )
    current_by_id: dict[int, dict[str, Any]] = {}
    current_by_identity: dict[tuple[str, str], dict[str, Any]] = {}
    duplicate_identities: set[tuple[str, str]] = set()
    for row in current_messages:
        immutable = _message_immutable(row)
        identity = (row["channel_id"], row["client_msg_id"])
        if row["id"] in current_by_id or identity in current_by_identity:
            duplicate_identities.add(identity)
        current_by_id[row["id"]] = immutable
        current_by_identity[identity] = immutable
    missing: list[tuple[str, str]] = []
    changed: list[tuple[str, str]] = []
    for expected in protected["messages"]:
        identity = tuple(expected["identity"])
        current = current_by_id.get(expected["id"])
        if (
            current is None
            or (current.get("channel_id"), current.get("client_msg_id")) != identity
        ):
            missing.append(identity)
        elif _hash(current) != expected["row_sha256"]:
            changed.append(identity)

    protected_writes = _loadgen_writes(run_dir, profile, session_planner)
    challenge_identities: set[tuple[str, str]] = set()
    for row in traffic_records:
        identity = (row.get("channel_id"), row.get("client_msg_id"))
        if any(not isinstance(value, str) or not value for value in identity):
            raise RestartSurvivorError(
                "restart survivor challenge traffic contains a malformed identity"
            )
        challenge_identities.add(identity)
    if len(challenge_identities) != len(traffic_records):
        raise RestartSurvivorError(
            "restart survivor challenge traffic contains duplicate identities"
        )
    baseline_identities = {
        tuple(expected["identity"]) for expected in protected["messages"]
    }
    accounted_identities = (
        baseline_identities | set(protected_writes) | challenge_identities
    )
    additional_identities = set(current_by_identity) - accounted_identities
    missing_writes: list[tuple[str, str]] = []
    changed_writes: list[tuple[str, str]] = []
    for identity, expected in protected_writes.items():
        current = current_by_identity.get(identity)
        if current is None:
            missing_writes.append(identity)
        elif profile == "p1_runtime":
            fixed_fields_match = all(
                current.get(key) == expected[key]
                for key in ("channel_id", "client_msg_id", "org_id")
            )
            allowed_states = expected.get(
                "_allowed_lifecycle_states",
                [
                    {
                        "body": expected["body"],
                        "edited": expected["_edited"],
                        "deleted": expected["_deleted"],
                    }
                ],
            )
            lifecycle_matches = any(
                current.get("body") == state["body"]
                and ((current.get("edited_at") is not None) is state["edited"])
                and ((current.get("deleted_at") is not None) is state["deleted"])
                for state in allowed_states
            )
            if not fixed_fields_match or not lifecycle_matches:
                changed_writes.append(identity)
        elif any(current.get(key) != value for key, value in expected.items()):
            changed_writes.append(identity)

    current_cursors = _rows(
        "SELECT coalesce(jsonb_agg(to_jsonb(x) ORDER BY channel_id),'[]'::jsonb)::text "
        "FROM (SELECT channel_id,last_seq FROM channel_seq) x;",
        "current conserved cursors",
        allow_empty=True,
    )
    cursor_by_channel: dict[str, int] = {}
    malformed_cursors = False
    for row in current_cursors:
        channel = row.get("channel_id")
        last_seq = row.get("last_seq")
        if (
            not isinstance(channel, str)
            or not channel
            or not isinstance(last_seq, int)
            or isinstance(last_seq, bool)
            or last_seq < 1
            or channel in cursor_by_channel
        ):
            malformed_cursors = True
            continue
        cursor_by_channel[channel] = last_seq
    max_rows = _rows(
        "SELECT coalesce(jsonb_agg(to_jsonb(x) ORDER BY channel_id),'[]'::jsonb)::text "
        "FROM (SELECT channel_id,max(seq)::bigint AS max_seq FROM messages GROUP BY channel_id) x;",
        "current conserved message maxima",
        allow_empty=True,
    )
    max_by_channel = {
        row.get("channel_id"): row.get("max_seq")
        for row in max_rows
        if isinstance(row.get("channel_id"), str)
        and isinstance(row.get("max_seq"), int)
        and not isinstance(row.get("max_seq"), bool)
    }
    if len(max_by_channel) != len(max_rows):
        raise RestartSurvivorError(
            "restart survivor current message maxima are malformed or duplicated"
        )
    missing_channels: list[str] = []
    regressed_channels: list[str] = []
    inconsistent_channels: list[str] = []
    baseline_cursors = {
        row["channel_id"]: row["last_seq"] for row in protected["cursors"]
    }
    growth_sequences: dict[str, list[int]] = {}
    for identity, row in current_by_identity.items():
        if identity not in baseline_identities:
            growth_sequences.setdefault(identity[0], []).append(row["seq"])
    non_contiguous_growth_channels = sorted(
        channel
        for channel, seqs in growth_sequences.items()
        if sorted(seqs)
        != list(
            range(
                baseline_cursors.get(channel, 0) + 1,
                baseline_cursors.get(channel, 0) + 1 + len(seqs),
            )
        )
    )
    required_channels = (
        set(baseline_cursors)
        | {identity[0] for identity in protected_writes}
        | {identity[0] for identity in challenge_identities}
        | set(max_by_channel)
    )
    for channel in sorted(required_channels):
        current = cursor_by_channel.get(channel)
        if current is None:
            missing_channels.append(channel)
        elif current < baseline_cursors.get(channel, 1):
            regressed_channels.append(channel)
        elif current != max_by_channel.get(channel):
            inconsistent_channels.append(channel)
    unexpected_cursor_channels = sorted(set(cursor_by_channel) - required_channels)

    current_outbox = (
        _rows(
            "SELECT coalesce(jsonb_agg(to_jsonb(x) ORDER BY channel_id,client_msg_id,effect_type),"
            "'[]'::jsonb)::text FROM (SELECT channel_id,client_msg_id,effect_type,payload "
            "FROM message_dispatch_outbox) x;",
            "current conserved outbox",
            allow_empty=True,
        )
        if _has_outbox(profile)
        else []
    )
    outbox_by_identity: dict[tuple[str, str, str], dict[str, Any]] = {}
    duplicate_outbox: set[tuple[str, str, str]] = set()
    for row in current_outbox:
        identity = (
            row.get("channel_id"),
            row.get("client_msg_id"),
            row.get("effect_type"),
        )
        if any(not isinstance(value, str) or not value for value in identity):
            raise RestartSurvivorError(
                "restart survivor current outbox identity is malformed"
            )
        if identity in outbox_by_identity:
            duplicate_outbox.add(identity)
        outbox_by_identity[identity] = row
    missing_outbox: list[tuple[str, str, str]] = []
    changed_outbox: list[tuple[str, str, str]] = []
    for expected in protected["outbox"]:
        identity = tuple(expected["identity"])
        current = outbox_by_identity.get(identity)
        if current is None:
            missing_outbox.append(identity)
        elif _hash(current) != expected["row_sha256"]:
            changed_outbox.append(identity)
    missing_write_outbox: list[tuple[str, str, str]] = []
    changed_write_outbox: list[tuple[str, str, str]] = []

    def publish_payload_matches(
        payload: Any,
        identity: tuple[str, str],
        message: dict[str, Any],
        expected_text: str | None,
    ) -> bool:
        if not isinstance(payload, dict):
            return False
        original_text = payload.get("text")
        text_matches = (
            original_text == expected_text
            if expected_text is not None
            else isinstance(original_text, str) and bool(original_text)
        )
        return bool(
            text_matches
            and payload.get("channel_id") == identity[0]
            and payload.get("client_msg_id") == identity[1]
            and payload.get("seq") == message.get("seq")
            and payload.get("org_id") == message.get("org_id")
        )

    if _has_outbox(profile):
        for identity in protected_writes:
            outbox_identity = (*identity, "publish-dispatch")
            row = outbox_by_identity.get(outbox_identity)
            if row is None:
                missing_write_outbox.append(outbox_identity)
                continue
            payload = row.get("payload")
            message = current_by_identity.get(identity, {})
            if not publish_payload_matches(
                payload, identity, message, protected_writes[identity]["_outbox_text"]
            ):
                changed_write_outbox.append(outbox_identity)
    missing_additional_outbox: list[tuple[str, str, str]] = []
    changed_additional_outbox: list[tuple[str, str, str]] = []
    if _has_outbox(profile):
        for identity in additional_identities:
            outbox_identity = (*identity, "publish-dispatch")
            row = outbox_by_identity.get(outbox_identity)
            if row is None:
                missing_additional_outbox.append(outbox_identity)
                continue
            payload = row.get("payload")
            message = current_by_identity[identity]
            if not publish_payload_matches(
                payload,
                identity,
                message,
                None if message.get("edited_at") is not None else message.get("body"),
            ):
                changed_additional_outbox.append(outbox_identity)

    baseline_outbox_identities = {
        tuple(expected["identity"]) for expected in protected["outbox"]
    }
    valid_publish_outbox = {
        (*identity, "publish-dispatch")
        for identity in set(current_by_identity)
    }
    unexpected_outbox = sorted(
        identity
        for identity in outbox_by_identity
        if identity not in baseline_outbox_identities
        and identity not in valid_publish_outbox
    )
    current_history_rows = (
        _rows(
            "SELECT coalesce(jsonb_agg(to_jsonb(x) ORDER BY id),'[]'::jsonb)::text "
            "FROM (SELECT id,service,control,created_at "
            "FROM service_control_history) x;",
            "current conserved control history",
            allow_empty=True,
        )
        if profile == "p1_runtime"
        else []
    )
    malformed_history_ids = [
        row.get("id")
        for row in current_history_rows
        if not _valid_history_row(row)
    ]
    valid_history_rows = [
        row
        for row in current_history_rows
        if _valid_history_row(row)
    ]
    current_history_by_id = {
        row["id"]: row
        for row in valid_history_rows
    }
    history_id_counts = Counter(row["id"] for row in valid_history_rows)
    duplicate_history_ids = sorted(
        row_id
        for row_id, count in history_id_counts.items()
        if count > 1
    )
    baseline_history_by_id = {
        row["id"]: row["row_sha256"] for row in protected["history"]["rows"]
    }
    missing_history_ids = sorted(
        set(baseline_history_by_id) - set(current_history_by_id)
    )
    changed_history_ids = sorted(
        row_id
        for row_id, expected_sha256 in baseline_history_by_id.items()
        if row_id in current_history_by_id
        and _hash(current_history_by_id[row_id]) != expected_sha256
    )
    non_append_history_ids = sorted(
        row_id
        for row_id in set(current_history_by_id) - set(baseline_history_by_id)
        if row_id <= protected["history"]["max_id"]
    )
    history_pass = not any(
        (
            malformed_history_ids,
            duplicate_history_ids,
            missing_history_ids,
            changed_history_ids,
            non_append_history_ids,
        )
    )

    def hashed(values: list[Any] | set[Any]) -> list[str]:
        return sorted(_hash(value) for value in values)

    passed = not any(
        (
            missing,
            changed,
            missing_writes,
            changed_writes,
            duplicate_identities,
            missing_channels,
            regressed_channels,
            inconsistent_channels,
            unexpected_cursor_channels,
            non_contiguous_growth_channels,
            malformed_cursors,
            missing_outbox,
            changed_outbox,
            missing_write_outbox,
            changed_write_outbox,
            missing_additional_outbox,
            changed_additional_outbox,
            unexpected_outbox,
            duplicate_outbox,
            not history_pass,
        )
    )
    result = {
        "pass": passed,
        "baseline_messages": {
            "pass": not (missing or changed or duplicate_identities),
            "expected": len(protected["messages"]),
            "surviving": len(protected["messages"]) - len(missing) - len(changed),
            "missing_identity_sha256": hashed(missing),
            "changed_identity_sha256": hashed(changed),
            "duplicate_identity_sha256": hashed(duplicate_identities),
        },
        "protected_writes": {
            "pass": not (missing_writes or changed_writes),
            "expected": len(protected_writes),
            "surviving": len(protected_writes)
            - len(missing_writes)
            - len(changed_writes),
            "missing_identity_sha256": hashed(missing_writes),
            "changed_identity_sha256": hashed(changed_writes),
        },
        "additional_messages": {
            "consistent": len(additional_identities)
            - len(missing_additional_outbox)
            - len(changed_additional_outbox),
            "observed": len(additional_identities),
            "identity_sha256": hashed(additional_identities),
        },
        "cursors": {
            "pass": not (
                missing_channels
                or regressed_channels
                or inconsistent_channels
                or unexpected_cursor_channels
                or non_contiguous_growth_channels
                or malformed_cursors
            ),
            "expected": len(required_channels),
            "present": len(required_channels) - len(missing_channels),
            "missing_channel_sha256": hashed(missing_channels),
            "regressed_channel_sha256": hashed(regressed_channels),
            "inconsistent_channel_sha256": hashed(inconsistent_channels),
            "unexpected_channel_sha256": hashed(unexpected_cursor_channels),
            "non_contiguous_growth_channel_sha256": hashed(
                non_contiguous_growth_channels
            ),
            "malformed_or_duplicate": malformed_cursors,
        },
        "outbox": {
            "pass": not (
                missing_outbox
                or changed_outbox
                or missing_write_outbox
                or changed_write_outbox
                or missing_additional_outbox
                or changed_additional_outbox
                or unexpected_outbox
                or duplicate_outbox
            ),
            "expected": (
                len(protected["outbox"])
                + len(protected_writes)
                + len(additional_identities)
            ),
            "surviving": len(protected["outbox"])
            + len(protected_writes)
            + len(additional_identities)
            - len(missing_outbox)
            - len(changed_outbox)
            - len(missing_write_outbox)
            - len(changed_write_outbox)
            - len(missing_additional_outbox)
            - len(changed_additional_outbox),
            "missing_identity_sha256": hashed(
                missing_outbox + missing_write_outbox + missing_additional_outbox
            ),
            "changed_identity_sha256": hashed(
                changed_outbox
                + changed_write_outbox
                + changed_additional_outbox
            ),
            "unexpected_identity_sha256": hashed(unexpected_outbox),
            "duplicate_identity_sha256": hashed(duplicate_outbox),
        },
        "history": {
            "pass": history_pass,
            "expected": len(protected["history"]["rows"]),
            "surviving": len(protected["history"]["rows"])
            - len(missing_history_ids)
            - len(changed_history_ids),
            "missing_id_sha256": hashed(missing_history_ids),
            "changed_id_sha256": hashed(changed_history_ids),
            "duplicate_id_sha256": hashed(duplicate_history_ids),
            "non_append_id_sha256": hashed(non_append_history_ids),
            "malformed_id_sha256": hashed(malformed_history_ids),
        },
    }
    if profile == "p1_runtime":
        result["interventions"] = _intervention_diagnostics(
            valid_history_rows,
            baseline_max_id=protected["history"]["max_id"],
        )
    return result


def verify_survival(
    run_dir: Path,
    profile: str,
    traffic_records: list[dict[str, Any]],
    session_planner: dict[str, Any] | None = None,
    database_setting_envelope: dict[str, Any] | None = None,
) -> dict[str, Any]:
    baseline = _baseline(run_dir, profile)
    conservation = _verify_conservation(
        run_dir, profile, baseline, traffic_records, session_planner
    )
    canary = baseline["canary"]
    channel, client = canary.get("channel_id"), canary.get("client_msg_id")
    if not _is_restart_sentinel(str(channel)) or not _is_restart_sentinel(str(client)):
        raise RestartSurvivorError("protected restart canary identity is malformed")
    queries = {
        "message_sha256": (
            "SELECT coalesce(jsonb_agg(to_jsonb(x)),'[]'::jsonb)::text FROM "
            f"(SELECT * FROM messages WHERE channel_id='{channel}' AND client_msg_id='{client}') x;"
        ),
        "cursor_sha256": (
            "SELECT coalesce(jsonb_agg(to_jsonb(x)),'[]'::jsonb)::text FROM "
            f"(SELECT * FROM channel_seq WHERE channel_id='{channel}') x;"
        ),
    }
    if _has_outbox(profile):
        queries["outbox_sha256"] = (
            "SELECT coalesce(jsonb_agg(to_jsonb(x)),'[]'::jsonb)::text FROM "
            "(SELECT * FROM message_dispatch_outbox "
            f"WHERE channel_id='{channel}' AND client_msg_id='{client}') x;"
        )
    canary_checks: dict[str, bool] = {}
    for name, query in queries.items():
        rows = _rows(query, f"current {name}", allow_empty=True)
        canary_checks[name] = len(rows) == 1 and _hash(rows[0]) == canary.get(name)

    clients = [row.get("client_msg_id") for row in traffic_records]
    if (
        not clients
        or any(
            not isinstance(value, str) or not value.startswith("v2-")
            for value in clients
        )
        or len(set(clients)) != len(clients)
    ):
        raise RestartSurvivorError("restart challenge traffic identities are malformed")
    quoted = ",".join("'" + value.replace("'", "''") + "'" for value in clients)
    messages = _rows(
        "SELECT coalesce(jsonb_agg(to_jsonb(x) ORDER BY client_msg_id),'[]'::jsonb)::text "
        "FROM (SELECT channel_id,client_msg_id,seq,body,org_id,deleted_at FROM messages "
        f"WHERE client_msg_id IN ({quoted})) x;",
        "restart traffic messages",
        allow_empty=True,
    )
    outbox = (
        _rows(
            "SELECT coalesce(jsonb_agg(to_jsonb(x) ORDER BY client_msg_id),'[]'::jsonb)::text "
            "FROM (SELECT channel_id,client_msg_id,effect_type,payload FROM message_dispatch_outbox "
            f"WHERE client_msg_id IN ({quoted})) x;",
            "restart traffic outbox",
            allow_empty=True,
        )
        if _has_outbox(profile)
        else []
    )
    expected = {
        row["client_msg_id"]: {
            "channel_id": row["channel_id"],
            "seq": row["seq"],
            "body": row["text"],
            "org_id": f"org-{row['channel_id']}",
            "deleted_at": None,
        }
        for row in traffic_records
    }
    observed = {
        row.get("client_msg_id"): {
            key: row.get(key)
            for key in ("channel_id", "seq", "body", "org_id", "deleted_at")
        }
        for row in messages
    }
    traffic_pass = observed == expected
    if _has_outbox(profile):
        traffic_pass = traffic_pass and {
            row.get("client_msg_id")
            for row in outbox
            if row.get("effect_type") == "publish-dispatch"
            and isinstance(row.get("payload"), dict)
            and row["payload"].get("seq")
            == expected.get(row.get("client_msg_id"), {}).get("seq")
        } == set(clients)
    current_scope = _scope()
    if profile == "lock_guard":
        database_oid = _psql(
            "SELECT oid FROM pg_database WHERE datname=current_database();"
        )
        app_oid = _psql("SELECT oid FROM pg_roles WHERE rolname='app';")
        if database_setting_envelope is None:
            scope = {
                "pass": _strip_idle_scope(
                    current_scope, database_oid=database_oid, app_oid=app_oid
                )
                == _strip_idle_scope(
                    baseline["scope"], database_oid=database_oid, app_oid=app_oid
                )
            }
        else:
            scope = compare_database_setting_scope(
                baseline["scope"],
                current_scope,
                database_setting_envelope,
                database_oid=database_oid,
                app_oid=app_oid,
            )
        scope_pass = scope["pass"] is True
        guard = lock_guard()
    else:
        scope_pass = current_scope == baseline["scope"]
        scope = {"pass": scope_pass}
        guard = None
    schema_pass = _schema(profile, require_nonempty=False) == baseline["schema"]
    history_pass = True
    p1_siblings = {"pass": True}
    p1_schema_pass = True
    p1_guardrail = None
    if profile == "p1_runtime":
        history_canary = canary.get("history_canary")
        history_pass = (
            _psql(
                "SELECT count(*) FROM service_control_history "
                f"WHERE service='grader' AND control='{history_canary}';"
            )
            == "1"
        )
        baseline_siblings = sorted(
            (row for row in baseline["p1_state"] if row.get("service") != "message"),
            key=lambda row: str(row.get("service")),
        )
        current_siblings = sorted(
            (
                row
                for row in _p1_state(allow_empty=True)
                if row.get("service") != "message"
            ),
            key=lambda row: str(row.get("service")),
        )
        p1_siblings = _p1_siblings_comparison(
            baseline_siblings, current_siblings
        )
        p1_schema_pass = _p1_schema(require_nonempty=False) == baseline.get("p1_schema")
        p1_guardrail = _p1_guardrail_probe()
    return {
        "pass": (
            all(canary_checks.values())
            and traffic_pass
            and conservation["pass"] is True
            and schema_pass
            and scope_pass
            and history_pass
            and p1_siblings["pass"] is True
            and p1_schema_pass
            and (p1_guardrail is None or p1_guardrail["pass"] is True)
        ),
        "canary": canary_checks,
        "traffic": {
            "expected": len(expected),
            "messages": len(messages),
            "outbox": len(outbox),
            "pass": traffic_pass,
        },
        "conservation": conservation,
        "schema": {"pass": schema_pass},
        "scope": scope,
        "history_canary": {"pass": history_pass},
        "p1_siblings": p1_siblings,
        "p1_schema": {"pass": p1_schema_pass},
        "p1_guardrail": p1_guardrail,
        "lock_guard": guard,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Capture a protected restart baseline")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--profile", required=True, choices=sorted(_PROFILES))
    args = parser.parse_args(argv)
    try:
        capture_baseline(args.output, args.profile)
    except Exception as exc:
        print(
            f"verifier restart survivor: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 2
    print("verifier restart survivor: protected baseline captured", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
