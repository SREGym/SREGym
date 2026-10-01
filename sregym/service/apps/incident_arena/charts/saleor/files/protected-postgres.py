"""Trusted helpers for scenario-owned Saleor PostgreSQL evidence collectors.

This module is mounted from the task chart into the hidden load-generator pod.
It is deliberately not present in, or used to rebuild, any substrate image.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

_IDENTIFIER = re.compile(r"[a-z_][a-z0-9_]*\Z")
_MAX_TABLES = 16
_MAX_ROWS_PER_TABLE = 50_000


def _canonical_digest(document: object) -> str:
    payload = json.dumps(
        document,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _identifier(value: object, field: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise RuntimeError(f"protected PostgreSQL collector has invalid {field}: {value!r}")
    return value


def _table_config(config: object) -> list[tuple[str, str, int]]:
    if not isinstance(config, dict) or set(config) != {"tables"}:
        raise RuntimeError(
            "protected PostgreSQL collector config must contain exactly a tables list"
        )
    raw_tables = config.get("tables")
    if not isinstance(raw_tables, list) or not raw_tables or len(raw_tables) > _MAX_TABLES:
        raise RuntimeError(
            f"protected PostgreSQL collector requires 1..{_MAX_TABLES} tables"
        )
    parsed: list[tuple[str, str, int]] = []
    for raw in raw_tables:
        if not isinstance(raw, dict) or set(raw) != {"schema", "name", "max_rows"}:
            raise RuntimeError(
                "each protected PostgreSQL table must contain schema, name, and max_rows"
            )
        schema = _identifier(raw["schema"], "table schema")
        name = _identifier(raw["name"], "table name")
        max_rows = raw["max_rows"]
        if (
            not isinstance(max_rows, int)
            or isinstance(max_rows, bool)
            or not 1 <= max_rows <= _MAX_ROWS_PER_TABLE
        ):
            raise RuntimeError(
                "protected PostgreSQL max_rows must be an integer in "
                f"[1, {_MAX_ROWS_PER_TABLE}]"
            )
        parsed.append((schema, name, max_rows))
    identities = [(schema, name) for schema, name, _ in parsed]
    if len(identities) != len(set(identities)):
        raise RuntimeError("protected PostgreSQL collector tables contain duplicates")
    return parsed


def _schema_document(connection: Any, schema: str, table: str) -> dict[str, Any]:
    relation = connection.execute(
        "SELECT c.oid, c.relkind, c.relpersistence, c.relrowsecurity, "
        "c.relforcerowsecurity, c.relreplident, owner.rolname, c.relacl::text "
        "FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "JOIN pg_roles owner ON owner.oid = c.relowner "
        "WHERE n.nspname = %s AND c.relname = %s",
        (schema, table),
    ).fetchone()
    if relation is None:
        raise RuntimeError(f"protected PostgreSQL table does not exist: {schema}.{table}")
    oid = int(relation[0])
    columns = connection.execute(
        "SELECT a.attnum, a.attname, format_type(a.atttypid, a.atttypmod), "
        "a.attnotnull, a.attidentity, a.attgenerated, coll.collname, "
        "pg_get_expr(def.adbin, def.adrelid) "
        "FROM pg_attribute a "
        "LEFT JOIN pg_attrdef def ON def.adrelid = a.attrelid AND def.adnum = a.attnum "
        "LEFT JOIN pg_collation coll ON coll.oid = a.attcollation AND a.attcollation <> 0 "
        "WHERE a.attrelid = %s AND a.attnum > 0 AND NOT a.attisdropped "
        "ORDER BY a.attnum",
        (oid,),
    ).fetchall()
    constraints = connection.execute(
        "SELECT conname, contype, condeferrable, condeferred, convalidated, "
        "pg_get_constraintdef(oid, true) FROM pg_constraint "
        "WHERE conrelid = %s ORDER BY conname",
        (oid,),
    ).fetchall()
    indexes = connection.execute(
        "SELECT idx.relname, ix.indisprimary, ix.indisunique, ix.indisvalid, "
        "ix.indisready, pg_get_indexdef(ix.indexrelid) "
        "FROM pg_index ix JOIN pg_class idx ON idx.oid = ix.indexrelid "
        "WHERE ix.indrelid = %s ORDER BY idx.relname",
        (oid,),
    ).fetchall()
    triggers = connection.execute(
        "SELECT tgname, tgenabled, pg_get_triggerdef(oid, true) FROM pg_trigger "
        "WHERE tgrelid = %s AND NOT tgisinternal ORDER BY tgname",
        (oid,),
    ).fetchall()
    policies = connection.execute(
        "SELECT polname, polcmd, polpermissive, polroles::text, "
        "pg_get_expr(polqual, polrelid), pg_get_expr(polwithcheck, polrelid) "
        "FROM pg_policy WHERE polrelid = %s ORDER BY polname",
        (oid,),
    ).fetchall()
    return {
        "relation": {
            "kind": str(relation[1]),
            "persistence": str(relation[2]),
            "row_security": bool(relation[3]),
            "force_row_security": bool(relation[4]),
            "replica_identity": str(relation[5]),
            "owner": str(relation[6]),
            "acl": None if relation[7] is None else str(relation[7]),
        },
        "columns": [
            {
                "position": int(row[0]),
                "name": str(row[1]),
                "type": str(row[2]),
                "not_null": bool(row[3]),
                "identity": str(row[4]),
                "generated": str(row[5]),
                "collation": None if row[6] is None else str(row[6]),
                "default": None if row[7] is None else str(row[7]),
            }
            for row in columns
        ],
        "constraints": [
            {
                "name": str(row[0]),
                "type": str(row[1]),
                "deferrable": bool(row[2]),
                "deferred": bool(row[3]),
                "validated": bool(row[4]),
                "definition": str(row[5]),
            }
            for row in constraints
        ],
        "indexes": [
            {
                "name": str(row[0]),
                "primary": bool(row[1]),
                "unique": bool(row[2]),
                "valid": bool(row[3]),
                "ready": bool(row[4]),
                "definition": str(row[5]),
            }
            for row in indexes
        ],
        "triggers": [
            {"name": str(row[0]), "enabled": str(row[1]), "definition": str(row[2])}
            for row in triggers
        ],
        "policies": [
            {
                "name": str(row[0]),
                "command": str(row[1]),
                "permissive": bool(row[2]),
                "roles": str(row[3]),
                "using": None if row[4] is None else str(row[4]),
                "check": None if row[5] is None else str(row[5]),
            }
            for row in policies
        ],
    }


def capture_bounded_relations(connection: Any, config: object) -> dict[str, Any]:
    """Hash every row and the complete relevant schema for bounded tables.

    The row cap is a proof boundary, not sampling: exceeding it fails the task
    instead of silently protecting only a subset. Each phase uses one read-only,
    repeatable-read transaction so row counts, contents, and schema agree.
    """

    tables = _table_config(config)
    result: dict[str, Any] = {"schema_version": 1, "tables": {}}
    with connection.transaction():
        connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        for schema, table, max_rows in tables:
            # Both identifiers passed the strict lowercase identifier grammar
            # above, so this quoting cannot introduce SQL syntax.
            relation = f'"{schema}"."{table}"'
            row_count = int(
                connection.execute(f"SELECT count(*) FROM {relation}").fetchone()[0]
            )
            if row_count > max_rows:
                raise RuntimeError(
                    f"protected PostgreSQL table {schema}.{table} has {row_count} rows, "
                    f"exceeding the complete-capture bound {max_rows}"
                )
            rows = [
                str(row[0])
                for row in connection.execute(
                    f"SELECT to_jsonb(value)::text FROM {relation} AS value"
                ).fetchall()
            ]
            if len(rows) != row_count:
                raise RuntimeError(
                    f"protected PostgreSQL table {schema}.{table} changed during capture"
                )
            schema_document = _schema_document(connection, schema, table)
            result["tables"][f"{schema}.{table}"] = {
                "row_count": row_count,
                "rows_sha256": _canonical_digest(sorted(rows)),
                "schema_sha256": _canonical_digest(schema_document),
            }
    return result
