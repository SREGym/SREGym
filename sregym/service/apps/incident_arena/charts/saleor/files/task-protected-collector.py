"""Protected role/database timeout evidence for the Saleor canary.

The collector runs only in the agent-inaccessible grading pod.  It records
semantic scope and fresh-session behavior, but never emits connection strings,
SQL text, PostgreSQL source paths, or source labels.
"""

from __future__ import annotations

import hashlib
from decimal import Decimal, InvalidOperation
from typing import Any

import psycopg

_CANCELLATION_SQLSTATE = "57014"
_TIME_UNITS_MS = {
    "": Decimal(1),
    "ms": Decimal(1),
    "s": Decimal(1_000),
    "min": Decimal(60_000),
    "h": Decimal(3_600_000),
    "d": Decimal(86_400_000),
}


def _config(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
        "application_dsn",
        "application_role",
        "database",
        "fault_timeout_ms",
        "max_order_rows",
        "order_table",
        "probe_duration_ms",
        "unrelated_dsn",
        "unrelated_role",
    }:
        raise RuntimeError(
            "timeout-scope collector config must contain exactly the two fresh "
            "connection identities, target scope, and timeout bounds"
        )
    for key in (
        "application_dsn",
        "application_role",
        "database",
        "order_table",
        "unrelated_dsn",
        "unrelated_role",
    ):
        if not isinstance(value[key], str) or not value[key]:
            raise RuntimeError(f"timeout-scope collector config {key} is invalid")
    for key in ("fault_timeout_ms", "probe_duration_ms", "max_order_rows"):
        if (
            not isinstance(value[key], int)
            or isinstance(value[key], bool)
            or value[key] <= 0
        ):
            raise RuntimeError(f"timeout-scope collector config {key} is invalid")
    if value["probe_duration_ms"] <= value["fault_timeout_ms"]:
        raise RuntimeError(
            "timeout-scope collector probe must exceed the injected timeout"
        )
    if value["application_role"] == value["unrelated_role"]:
        raise RuntimeError(
            "timeout-scope collector application and unrelated roles must differ"
        )
    table = value["order_table"]
    if (
        table != table.lower()
        or not (table[0].isalpha() or table[0] == "_")
        or any(not (character.isalnum() or character == "_") for character in table)
        or value["max_order_rows"] > 50_000
    ):
        raise RuntimeError("timeout-scope collector order table configuration is invalid")
    return value


def _timeout_ms(raw: object) -> int:
    if not isinstance(raw, str) or not raw.strip():
        raise RuntimeError("timeout-scope collector received an invalid timeout")
    text = raw.strip().lower()
    unit = ""
    for candidate in ("min", "ms", "s", "h", "d"):
        if text.endswith(candidate):
            unit = candidate
            text = text[: -len(candidate)].strip()
            break
    try:
        milliseconds = Decimal(text) * _TIME_UNITS_MS[unit]
    except (InvalidOperation, KeyError) as exc:
        raise RuntimeError(
            f"timeout-scope collector cannot normalize timeout {raw!r}"
        ) from exc
    if (
        not milliseconds.is_finite()
        or milliseconds < 0
        or milliseconds != milliseconds.to_integral_value()
    ):
        raise RuntimeError(
            f"timeout-scope collector timeout is not a nonnegative whole millisecond: {raw!r}"
        )
    return int(milliseconds)


def _fresh_session(
    dsn: str,
    *,
    expected_role: str,
    expected_database: str,
    probe_duration_ms: int | None,
) -> dict[str, Any]:
    with psycopg.connect(dsn, connect_timeout=10) as connection:
        row = connection.execute(
            "SELECT current_user, current_database(), setting, unit "
            "FROM pg_settings WHERE name = 'statement_timeout'"
        ).fetchone()
        if row is None or len(row) != 4:
            raise RuntimeError(
                "timeout-scope collector fresh session returned no timeout setting"
            )
        role, database, setting, unit = row
        if str(role) != expected_role or str(database) != expected_database:
            raise RuntimeError(
                "timeout-scope collector fresh connection identity mismatch"
            )
        if str(unit) != "ms":
            raise RuntimeError(
                f"timeout-scope collector expected statement_timeout unit ms, got {unit!r}"
            )
        timeout_ms = _timeout_ms(str(setting))
        result: dict[str, Any] = {
            "role": str(role),
            "database": str(database),
            "statement_timeout_ms": timeout_ms,
        }
        if probe_duration_ms is None:
            return result

        completed = False
        sqlstate: str | None = None
        try:
            connection.execute(
                "SELECT pg_sleep(%s)", (Decimal(probe_duration_ms) / Decimal(1_000),)
            ).fetchone()
            completed = True
        except psycopg.Error as exc:
            sqlstate = exc.sqlstate
            if sqlstate != _CANCELLATION_SQLSTATE:
                raise RuntimeError(
                    "timeout-scope collector fresh-session probe failed for an "
                    f"unexpected reason (SQLSTATE {sqlstate!r})"
                ) from exc
            connection.rollback()
        result["probe"] = {"completed": completed, "sqlstate": sqlstate}
        return result


def _scoped_settings(connection: Any) -> list[dict[str, Any]]:
    rows = connection.execute(
        "SELECT db.datname, rol.rolname, "
        "substring(item.setting FROM position('=' IN item.setting) + 1) "
        "FROM pg_db_role_setting AS scoped "
        "LEFT JOIN pg_database AS db ON db.oid = scoped.setdatabase "
        "LEFT JOIN pg_roles AS rol ON rol.oid = scoped.setrole "
        "CROSS JOIN LATERAL unnest(scoped.setconfig) AS item(setting) "
        "WHERE split_part(item.setting, '=', 1) = 'statement_timeout' "
        "ORDER BY db.datname NULLS FIRST, rol.rolname NULLS FIRST, item.setting"
    ).fetchall()
    result = [
        {
            "database": None if database is None else str(database),
            "role": None if role is None else str(role),
            "statement_timeout_ms": _timeout_ms(str(setting)),
        }
        for database, role, setting in rows
    ]
    identities = [(row["database"], row["role"]) for row in result]
    if len(identities) != len(set(identities)):
        raise RuntimeError(
            "timeout-scope collector found duplicate role/database timeout settings"
        )
    return result


def _file_settings(connection: Any) -> list[dict[str, Any]]:
    rows = connection.execute(
        "SELECT setting, applied, error "
        "FROM pg_file_settings WHERE name = 'statement_timeout' ORDER BY seqno"
    ).fetchall()
    result: list[dict[str, Any]] = []
    for setting, applied, error in rows:
        if error is not None:
            raise RuntimeError(
                "timeout-scope collector found an invalid persisted statement timeout"
            )
        if not isinstance(applied, bool):
            raise RuntimeError(
                "timeout-scope collector found malformed pg_file_settings evidence"
            )
        result.append(
            {
                "statement_timeout_ms": _timeout_ms(str(setting)),
                "applied": applied,
            }
        )
    if not result:
        raise RuntimeError(
            "timeout-scope collector found no persisted global timeout baseline"
        )
    return result


def _orders(connection: Any, table: str, max_rows: int) -> list[dict[str, str]]:
    relation = f'"{table}"'
    count = int(connection.execute(f"SELECT count(*) FROM {relation}").fetchone()[0])
    if count < 1 or count > max_rows:
        raise RuntimeError(
            f"timeout-scope collector protected order count {count} is outside 1..{max_rows}"
        )
    rows = connection.execute(
        f"SELECT id::text, to_jsonb(protected_order)::text "
        f"FROM {relation} AS protected_order ORDER BY id"
    ).fetchall()
    if len(rows) != count:
        raise RuntimeError(
            "timeout-scope collector protected orders changed during capture"
        )
    result: list[dict[str, str]] = []
    for identity, payload in rows:
        if not isinstance(identity, str) or not identity or not isinstance(payload, str):
            raise RuntimeError(
                "timeout-scope collector found malformed protected order content"
            )
        result.append(
            {
                "identity": identity,
                "sha256": hashlib.sha256(payload.encode()).hexdigest(),
            }
        )
    identities = [row["identity"] for row in result]
    if len(identities) != len(set(identities)):
        raise RuntimeError(
            "timeout-scope collector found duplicate protected order identities"
        )
    return result


def capture(
    *, connection: Any, config: object, phase: str
) -> dict[str, Any]:
    """Capture target scope, global declarations, and two new DB sessions."""

    if phase not in {"before", "after"}:
        raise RuntimeError(f"timeout-scope collector phase is invalid: {phase!r}")
    parsed = _config(config)
    return {
        "schema_version": 1,
        "scoped_settings": _scoped_settings(connection),
        "file_settings": _file_settings(connection),
        "orders": _orders(
            connection,
            parsed["order_table"],
            parsed["max_order_rows"],
        ),
        "fresh_application": _fresh_session(
            parsed["application_dsn"],
            expected_role=parsed["application_role"],
            expected_database=parsed["database"],
            probe_duration_ms=parsed["probe_duration_ms"],
        ),
        "fresh_unrelated": _fresh_session(
            parsed["unrelated_dsn"],
            expected_role=parsed["unrelated_role"],
            expected_database=parsed["database"],
            probe_duration_ms=None,
        ),
    }
