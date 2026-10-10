"""Fixer-visible telemetry signals used by the observability gate.

The DeathStarBench apps export no span metrics, so the golden-signal alerts
rarely fire for them. Besides new Prometheus alerts, the gate therefore
accepts a clear surge of error-like log lines in Loki for some container of
the application, measured against the same window before injection.
"""

from __future__ import annotations

import json
import subprocess
import urllib.parse

LOKI = "http://loki.observe.svc.cluster.local:3100"
ERROR_PATTERN = (
    "(?i)(error|fail|exception|unavailable|refused|timeout|timed out|denied|panic|unimplemented|reset by peer)"
)


def _wget_from_prometheus(url: str) -> dict | None:
    # Same access path as AlertOracle: no dependency on cluster DNS from the host.
    cmd = ["kubectl", "-n", "observe", "exec", "deploy/prometheus-server", "-c", "prometheus-server", "--",
           "wget", "-qO-", url]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        return json.loads(out.stdout) if out.returncode == 0 else None
    except (subprocess.TimeoutExpired, json.JSONDecodeError):
        return None


def error_log_counts(namespace: str, window_s: int = 120) -> dict[str, float]:
    """Error-like log lines per container over the last ``window_s`` seconds."""
    query = (
        f'sum by (container) (count_over_time({{namespace="{namespace}"}} |~ "{ERROR_PATTERN}" [{int(window_s)}s]))'
    )
    data = _wget_from_prometheus(f"{LOKI}/loki/api/v1/query?query={urllib.parse.quote(query)}")
    if not data or data.get("status") != "success":
        return {}
    return {
        r["metric"].get("container", "?"): float(r["value"][1]) for r in data.get("data", {}).get("result", [])
    }


def log_surges(before: dict[str, float], after: dict[str, float], min_lines: int = 30,
               factor: float = 3.0) -> dict[str, list[float]]:
    """Containers whose error-line count rose clearly above their own baseline."""
    return {
        container: [before.get(container, 0.0), count]
        for container, count in after.items()
        if count >= min_lines and count >= factor * before.get(container, 0.0) + min_lines
    }
