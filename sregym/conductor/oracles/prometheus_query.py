"""Query Prometheus from inside the observe prometheus-server pod."""

from __future__ import annotations

import json
import subprocess
from urllib.parse import quote

_PROMETHEUS_URL = "http://localhost:9090"


def prometheus_query_url(query: str) -> str:
    return f"{_PROMETHEUS_URL}/api/v1/query?query={quote(query, safe='')}"


def prometheus_scalar(query: str, *, announce: bool = True) -> float | None:
    """Return the summed instant value for a PromQL query, or None on failure."""
    cmd = [
        "kubectl",
        "exec",
        "-n",
        "observe",
        "deploy/prometheus-server",
        "-c",
        "prometheus-server",
        "--",
        "wget",
        "-qO-",
        "-T",
        "10",
        prometheus_query_url(query),
    ]
    try:
        raw = subprocess.check_output(cmd, text=True, timeout=20)
        payload = json.loads(raw)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        if announce:
            print(f"[FAIL] Prometheus query failed: {exc}")
        return None
    if payload.get("status") != "success":
        if announce:
            print("[FAIL] Prometheus returned an error status")
        return None
    result = payload.get("data", {}).get("result") or []
    if not result:
        return 0.0
    total = 0.0
    for series in result:
        value = series.get("value") or [None, None]
        try:
            total += float(value[1])
        except (TypeError, ValueError, IndexError):
            if announce:
                print("[FAIL] Prometheus sample was not a number")
            return None
    return total


# Client spans land on recommendation; server spans land on product-catalog.
# Either series is enough to see the 10x fan-out. Frontend catalog browsing is
# excluded so load-generator product-page traffic cannot mask the fault.
_LIST_PRODUCTS_MATCHER = (
    'span_name=~".*ListProducts.*",service_name=~".*(product-catalog|recommendation).*"'
)


def catalog_list_products_total(namespace: str) -> float | None:
    """Count ListProducts spans on catalog or recommendation, failing closed if Prom is down."""
    matcher = _LIST_PRODUCTS_MATCHER
    scoped = prometheus_scalar(
        f'sum(traces_span_metrics_calls_total{{{matcher},namespace="{namespace}"}})',
        announce=False,
    )
    if scoped is not None:
        return scoped
    return prometheus_scalar(
        f"sum(traces_span_metrics_calls_total{{{matcher}}})",
        announce=True,
    )
