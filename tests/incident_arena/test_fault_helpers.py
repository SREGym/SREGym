"""Pure helpers used to inject and grade Incident Arena faults."""

import pytest

from sregym.conductor.problems.incident_arena.frappe import render_start_script, unlimited_or_at_least
from sregym.conductor.problems.incident_arena.saleor import safe_statement_timeout, timeout_ms
from sregym.conductor.problems.incident_arena.slack_spine import (
    flatten,
    in_safe_maintenance_window,
    patch_role_db_config,
)

START_SCRIPT = """#!/bin/bash
ARGS=("--port" "${REDIS_PORT}")
ARGS+=("--protected-mode" "no")
ARGS+=("--include" "/opt/bitnami/redis/etc/redis.conf")
ARGS+=("--include" "/opt/bitnami/redis/etc/master.conf")
ARGS+=("--maxmemory")
ARGS+=("64mb")
ARGS+=("--maxmemory-policy")
ARGS+=("noeviction")
exec redis-server "${ARGS[@]}"
"""


def test_start_script_flags_are_replaced_in_place():
    rendered = render_start_script(START_SCRIPT, ["--maxmemory", "2mb", "--user", "default", "on", "&*", "-blpop"])
    lines = rendered.splitlines()
    assert lines[4] == 'ARGS+=("--include" "/opt/bitnami/redis/etc/master.conf")'
    assert lines[5:12] == [
        'ARGS+=("--maxmemory")',
        'ARGS+=("2mb")',
        'ARGS+=("--user")',
        'ARGS+=("default")',
        'ARGS+=("on")',
        'ARGS+=("&*")',
        'ARGS+=("-blpop")',
    ]
    assert lines[-1].startswith("exec redis-server")
    # Round trip back to the healthy flags restores the original script.
    assert render_start_script(rendered, ["--maxmemory", "64mb", "--maxmemory-policy", "noeviction"]) == START_SCRIPT


def test_start_script_layout_is_validated():
    with pytest.raises(RuntimeError):
        render_start_script("#!/bin/bash\nexec something\n", ["--x"])


APP_YAML = """roles:
  auth:
    db:
      pool_size: 20
      max_overflow: 10
      pool_timeout_s: 30
      # a comment that must survive
    mesh:
      retries: 1
  channel:
    db:
      pool_size: 20
      max_overflow: 10
      hold_ms: 10
    mesh:
      retries: 1
"""


def test_role_pool_patch_touches_only_the_target_role():
    patched = patch_role_db_config(APP_YAML, "channel", {"pool_size": 3, "max_overflow": 2})
    assert "      # a comment that must survive" in patched
    lines = patched.splitlines()
    assert lines[3:5] == ["      pool_size: 20", "      max_overflow: 10"]
    assert lines[11:13] == ["      pool_size: 3", "      max_overflow: 2"]
    assert patched.endswith("\n")


def test_role_pool_patch_fails_loudly_on_unknown_keys():
    with pytest.raises(RuntimeError):
        patch_role_db_config(APP_YAML, "message", {"pool_size": 3})


def test_flatten_admin_config():
    assert flatten({"role": "x", "db": {"pool_size": 3}, "mesh": {"retries": 1}}) == {
        "role": "x",
        "db.pool_size": 3,
        "mesh.retries": 1,
    }


@pytest.mark.parametrize(
    ("offset", "safe"),
    [(0, True), (22, True), (23, False), (35, False), (49, False), (50, True), (55, True), (59.5, True), (60, False)],
)
def test_maintenance_windows_match_incident_arena_bounds(offset, safe):
    assert in_safe_maintenance_window(offset) is safe


@pytest.mark.parametrize(
    ("value", "ms", "safe"),
    [("0", 0, True), ("150ms", 150, False), ("750ms", 750, False), ("2s", 2000, True), ("1min", 60000, True)],
)
def test_statement_timeout_parsing(value, ms, safe):
    assert timeout_ms(value) == ms
    assert safe_statement_timeout(value) is safe


def test_connection_cap_acceptance():
    accept = unlimited_or_at_least(16)
    assert accept("0") and accept("16") and accept("500")
    assert not accept("8") and not accept("garbage")
