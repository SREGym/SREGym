"""Query Prometheus from inside the observe prometheus-server pod."""

from __future__ import annotations

import json
import subprocess
from urllib.parse import quote

_PROMETHEUS_URL = "http://localhost:9090"
_LIST_PRODUCTS_MATCHER = 'span_name=~".*ListProducts.*",service_name=~".*product-catalog.*"'
_LIST_RECOMMENDATIONS_MATCHER = (
    'span_name=~".*ListRecommendations.*",service_name=~".*recommendation.*"'
)


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


def _namespaced_span_total(namespace: str, matcher: str) -> float | None:
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


def catalog_list_products_total(namespace: str) -> float | None:
    """Count product-catalog ListProducts spans, failing closed if Prom is down."""
    return _namespaced_span_total(namespace, _LIST_PRODUCTS_MATCHER)


def list_recommendations_total(namespace: str) -> float | None:
    """Count recommendation ListRecommendations spans, failing closed if Prom is down."""
    return _namespaced_span_total(namespace, _LIST_RECOMMENDATIONS_MATCHER)
