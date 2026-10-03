"""SREGym problems ported to the Incident Arena apps (environment scaling)."""

from __future__ import annotations

# Original registry id -> (ported problem id, problem class). Variants of one
# fault on different original apps map to the same port.
PORTS: dict[str, tuple[str, type]] = {}
