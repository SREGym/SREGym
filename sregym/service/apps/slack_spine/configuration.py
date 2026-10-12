"""Slack Spine configuration updates and capacity bounds."""

from __future__ import annotations

import re

HEALTHY_POOL = {"pool_size": 20, "max_overflow": 10}
# Repairs must remain within the peer roles' connection-pool budget.
POOL_CEILING = {"pool_size": 20, "max_overflow": 10}
POOL_TIMEOUT_FLOOR_S = 1
# Healthy handler delays from the application chart.
HEALTHY_HOLD_MS = {
    "auth": 5,
    "workspace": 8,
    "channel": 10,
    "message": 150,
    "thread": 12,
    "file": 8,
    "search": 8,
    "notification": 8,
    "platform": 8,
}


def patch_role_db_config(text: str, role: str, updates: dict[str, int]) -> str:
    """Rewrite ``roles.<role>.db.<key>`` values in the rendered app.yaml, keeping comments."""
    lines = text.splitlines()
    out = []
    in_role = in_db = False
    seen = set()
    for line in lines:
        if re.match(r"^  \S", line):
            in_role = line.strip() == f"{role}:"
            in_db = False
        elif in_role and re.match(r"^    \S", line):
            in_db = line.strip() == "db:"
        elif in_role and in_db:
            match = re.match(r"^(      )([a-z_]+):(\s*)(\S.*)$", line)
            if match and match.group(2) in updates:
                key = match.group(2)
                line = f"{match.group(1)}{key}: {updates[key]}"
                seen.add(key)
        out.append(line)
    missing = set(updates) - seen
    if missing:
        raise RuntimeError(f"app.yaml has no roles.{role}.db keys {sorted(missing)}")
    return "\n".join(out) + ("\n" if text.endswith("\n") else "")


def flatten(payload, prefix: str = "") -> dict[str, object]:
    """``{"db": {"pool_size": 20}}`` -> ``{"db.pool_size": 20}``."""
    flat = {}
    for key, value in (payload or {}).items():
        path = f"{prefix}{key}"
        if isinstance(value, dict):
            flat.update(flatten(value, f"{path}."))
        else:
            flat[path] = value
    return flat


def in_safe_maintenance_window(offset_s: float, period_s: float = 60, duration_s: float = 8) -> bool:
    """Incident Arena's safe offsets for the 60s cycle (30s warmup, 20s write peak).

    A complete run must finish before the peak starts (offset <= 22) or start
    after it ends (50 <= offset < 60).
    """
    return (0 <= offset_s <= 22) or (50 <= offset_s < period_s)
