"""Frappe database and Redis queue configuration helpers."""

from __future__ import annotations

import re
from collections.abc import Callable

from sregym.service.apps.frappe.application import Frappe

MIB = 1024 * 1024
# The imported contract requires a bounded, loss-intolerant queue.
MAXMEMORY_BAND = (16 * MIB, 128 * MIB)
HEALTHY_QUEUE_FLAGS = ["--maxmemory", "64mb", "--maxmemory-policy", "noeviction"]
DEQUEUE_COMMANDS = ("blpop", "blmove", "brpop")


PROTECTED_GLOBALS = (
    "max_connections",
    "max_user_connections",
    "wait_timeout",
    "read_only",
    "performance_schema",
    "init_connect",
    "max_statement_time",
    "innodb_buffer_pool_size",
    "transaction_isolation",
    "max_allowed_packet",
    "sql_mode",
)


SYSTEM_SCHEMAS = "('information_schema','mysql','performance_schema','sys')"


def site_database(app: Frappe) -> str:
    db = app.mysql(
        "SELECT TABLE_SCHEMA FROM information_schema.TABLES WHERE TABLE_NAME = 'tabDocType' "
        f"AND TABLE_SCHEMA NOT IN {SYSTEM_SCHEMAS} LIMIT 1"
    ).strip()
    if not db or "`" in db:
        raise RuntimeError(f"could not identify the Frappe site database (got {db!r})")
    return db


def site_account(app: Frappe, database: str) -> str:
    """The non-root account holding SELECT on the site schema, e.g. ``'_5e5..'@'%'``."""
    grantee = app.mysql(
        "SELECT GRANTEE FROM information_schema.SCHEMA_PRIVILEGES "
        f"WHERE TABLE_SCHEMA = '{database}' AND PRIVILEGE_TYPE = 'SELECT' "
        "AND GRANTEE NOT LIKE '''root''@%' LIMIT 1"
    ).strip()
    if not grantee:
        raise RuntimeError(f"no site account holds SELECT on {database}")
    return grantee


def schema_privileges(app: Frappe, database: str, grantee: str) -> list[str]:
    rows = app.mysql(
        "SELECT PRIVILEGE_TYPE FROM information_schema.SCHEMA_PRIVILEGES "
        f"WHERE TABLE_SCHEMA = '{database}' AND GRANTEE = {_sql_str(grantee)} ORDER BY 1"
    )
    return [r for r in rows.splitlines() if r]


def grant_fingerprint(app: Frappe) -> list[str]:
    """Every global/schema/table privilege row on the server, ``|``-separated.

    (``mysql -B`` escapes TABs inside a column, so a single-column row cannot
    use them as a separator.)
    """
    rows = app.mysql(
        "SELECT CONCAT_WS('|', 'user', GRANTEE, PRIVILEGE_TYPE, IS_GRANTABLE) FROM information_schema.USER_PRIVILEGES "
        "UNION ALL SELECT CONCAT_WS('|', 'schema', GRANTEE, TABLE_SCHEMA, PRIVILEGE_TYPE, IS_GRANTABLE) "
        "FROM information_schema.SCHEMA_PRIVILEGES "
        "UNION ALL SELECT CONCAT_WS('|', 'table', GRANTEE, CONCAT(TABLE_SCHEMA, '.', TABLE_NAME), PRIVILEGE_TYPE) "
        "FROM information_schema.TABLE_PRIVILEGES"
    )
    return sorted(r for r in rows.splitlines() if r)


def global_variables(app: Frappe, names) -> dict[str, str]:
    in_list = ",".join(_sql_str(n) for n in names)
    rows = app.mysql(
        f"SELECT VARIABLE_NAME, VARIABLE_VALUE FROM information_schema.GLOBAL_VARIABLES WHERE VARIABLE_NAME IN ({in_list.upper()})"
    )
    out = {}
    for row in rows.splitlines():
        name, _, value = row.partition("\t")
        if name:
            out[name.lower()] = value
    return out


def _sql_str(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "''") + "'"


def redis_config(app: Frappe, key: str, cache: bool = False) -> str:
    lines = app.redis("CONFIG", "GET", key, cache=cache).strip().splitlines()
    return lines[1].strip() if len(lines) >= 2 else ""


def redis_acl_rules(app: Frappe, user: str = "default") -> list[str]:
    """Command rules of a Redis ACL user, e.g. ``['+@all', '-blpop']``."""
    out = app.redis("ACL", "GETUSER", user).strip().splitlines()
    for index, line in enumerate(out):
        if line.strip() == "commands" and index + 1 < len(out):
            return out[index + 1].strip().split()
    return []


def render_start_script(script: str, flags: list[str]) -> str:
    """Replace the extraFlags lines of the Bitnami ``start-master.sh`` with ``flags``.

    The chart renders each extra flag as ``ARGS+=("<flag>")`` between the
    ``--include .../master.conf`` line and ``exec redis-server``.
    """
    lines = script.splitlines()
    try:
        start = next(i for i, line in enumerate(lines) if 'master.conf")' in line and line.startswith("ARGS+="))
        end = next(i for i, line in enumerate(lines) if line.startswith("exec redis-server"))
    except StopIteration as exc:
        raise RuntimeError("unrecognised Bitnami redis start-master.sh layout") from exc
    rendered = ['ARGS+=("' + flag.replace("\\", "\\\\").replace('"', '\\"') + '")' for flag in flags]
    return "\n".join([*lines[: start + 1], *rendered, *lines[end:]]) + "\n"


def unlimited_or_at_least(minimum: int) -> Callable[[str], bool]:
    def accept(value: str) -> bool:
        try:
            number = int(value)
        except ValueError:
            return False
        return number == 0 or number >= minimum

    return accept


def is_off(value: str) -> bool:
    return value.upper() in ("OFF", "0")


def _info_field(info: str, field: str) -> str:
    match = re.search(rf"^{re.escape(field)}:(\S+)", info, re.MULTILINE)
    return match.group(1) if match else ""
