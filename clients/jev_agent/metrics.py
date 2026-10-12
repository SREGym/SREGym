"""Prometheus time series for one component, summarised into short factual lines.

Each family is a fixed set of PromQL templates over the component's pods or service name, evaluated as
`[15m:30s]` subqueries (the Prometheus MCP tool only runs instant queries, and a subquery yields a matrix).
Series are summarised in code (baseline, recent, max, change ratio, onset); the model never sees raw points.

Pure helpers (parse_prom, summarize_series, detect_span_label, the query builders) take a `call(query) -> str`
function so they can be tested without a cluster.
"""

from __future__ import annotations

import contextvars
import logging
import math
import re
import statistics
import time
from collections.abc import Callable, Sequence
from typing import Any

from clients.jev_agent import mcp
from clients.jev_agent.types import AgentContext, ToolResult
from clients.jev_diag.checks import parse_cpu_millis, parse_memory_bytes
from clients.jev_diag.collector import WARMUP_SECONDS
from clients.jev_diag.timeutil import parse_time

logger = logging.getLogger("all.jev_agent.metrics")

PromCall = Callable[[str], str]

WINDOW = "15m"
STEP = "30s"
RANGE = f"[{WINDOW}:{STEP}]"
SPAN_CALLS = "traces_span_metrics_calls_total"
SPAN_BUCKETS = "traces_span_metrics_duration_milliseconds_bucket"
FAMILIES = ("cpu", "memory", "restarts", "rpc", "probe", "app")
MAX_APP_CANDIDATES = 10
DEFAULT_RATE_WINDOW = "4m"
SPAN_RECHECK_S = 30.0
SPAN_SERVICES_TTL_S = 60.0
MIN_POINTS_AFTER_WARMUP = 6
# Range points earlier than this (epoch seconds) are the component's start-up and are left out of summaries.
_WARM_END: contextvars.ContextVar[float | None] = contextvars.ContextVar("warm_end", default=None)
_LIMIT_NAME_RE = re.compile(r"(capacity|limit|max|maximum|size|quota)(_[a-z]+)?$")
ONSET_MADS = 3.0
_INFRA_METRIC_RE = (
    "container_.*|kube_.*|machine_.*|node_.*|probe_.*|up|scrape_.*|traces_.*|go_.*|process_.*|promhttp_.*|"
    "otelcol_.*|prometheus_.*"
)


# --------------------------------------------------------------------------- parsing


def _num(value: Any) -> float | None:
    """A value as a finite float, else None."""
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def parse_prom(text: str | None) -> list[dict]:
    """get_metrics text (Python repr of Prometheus `data`) -> [{"labels": {...}, "points": [(ts, value), ...]}].

    Vector and scalar results give one point per series; matrix results give the full series. Non-finite values
    ("NaN", "+Inf") are dropped.
    """
    data = mcp.parse_py_literal(text) if isinstance(text, str) else text
    if not isinstance(data, dict):
        return []
    rtype, result = data.get("resultType"), data.get("result")
    if rtype == "scalar" and isinstance(result, list) and len(result) == 2:
        v = _num(result[1])
        return [{"labels": {}, "points": [(float(result[0]), v)] if v is not None else []}]
    out = []
    for item in result if isinstance(result, list) else []:
        if not isinstance(item, dict):
            continue
        raw = item.get("values") if "values" in item else ([item["value"]] if item.get("value") else [])
        points = []
        for pair in raw or []:
            if not isinstance(pair, list | tuple) or len(pair) != 2:
                continue
            ts, v = _num(pair[0]), _num(pair[1])
            if ts is not None and v is not None:
                points.append((ts, v))
        warm_end = _WARM_END.get()
        if warm_end is not None and "values" in item:
            settled = [pt for pt in points if pt[0] >= warm_end]
            if len(settled) >= MIN_POINTS_AFTER_WARMUP:
                points = settled
        out.append({"labels": dict(item.get("metric") or {}), "points": points})
    return out


# --------------------------------------------------------------------------- summarising


def summarize_series(values: Sequence[float | tuple[float, float] | list]) -> dict:
    """Robust summary of one series given as values or (timestamp, value) pairs.

    baseline = median of the first third, recent = median of the last third, max/min/last, change = recent /
    baseline (None when the baseline is 0), delta = recent - baseline. The onset is the first point after the
    baseline window that lies more than ONSET_MADS robust deviations (MAD, floored at 5% of |baseline| and
    1e-9) from the baseline and is followed by another such point (or is the last point); `onset_s_before_end`
    is its distance from the last point. NaN/inf values are ignored; with fewer than 3 points no onset is set.
    """
    pts: list[tuple[float, float]] = []
    for i, item in enumerate(values or []):
        if isinstance(item, list | tuple) and len(item) == 2:
            ts, v = _num(item[0]), _num(item[1])
        else:
            ts, v = float(i), _num(item)
        if ts is not None and v is not None:
            pts.append((ts, v))
    if not pts:
        return {"n": 0}
    vals = [v for _, v in pts]
    n = len(vals)
    third = max(1, n // 3)
    baseline = statistics.median(vals[:third])
    recent = statistics.median(vals[-third:])
    out: dict[str, Any] = {
        "n": n,
        "baseline": baseline,
        "recent": recent,
        "max": max(vals),
        "min": min(vals),
        "last": vals[-1],
        "delta": recent - baseline,
        "change": (recent / baseline) if baseline else None,
        "span_s": pts[-1][0] - pts[0][0],
    }
    if n < 3:
        out["onset_index"] = None
        return out
    base_vals = vals[:third]
    mad = statistics.median([abs(v - baseline) for v in base_vals])
    scale = max(mad, 0.05 * abs(baseline), 1e-9)
    beyond = [abs(v - baseline) > ONSET_MADS * scale for v in vals]
    onset = None
    for i in range(third, n):
        if beyond[i] and (i == n - 1 or beyond[i + 1]):
            onset = i
            break
    out["onset_index"] = onset
    if onset is not None:
        out["onset_direction"] = "up" if vals[onset] > baseline else "down"
        out["onset_s_before_end"] = int(pts[-1][0] - pts[onset][0])
    return out


def describe_summary(label: str, s: dict, fmt: Callable[[float], str], extra: str = "") -> str:
    """One line: 'label: recent X (baseline Y, x2.3), max Z; rose ~180s before the latest sample'."""
    if not s.get("n"):
        return f"{label}: no samples"
    text = f"{label}: recent {fmt(s['recent'])} (baseline {fmt(s['baseline'])}"
    if s.get("change") is not None and abs(s["change"] - 1) >= 0.1:
        text += f", x{s['change']:.2g}"
    text += f"), max {fmt(s['max'])}"
    if s.get("onset_index") is not None:
        verb = "rose" if s.get("onset_direction") == "up" else "fell"
        text += f"; {verb} ~{s['onset_s_before_end']}s before the latest sample"
    elif s.get("n", 0) >= 3:
        text += "; no shift from baseline"
    return text + extra


def _fmt_cores(v: float) -> str:
    """CPU cores as millicores ('250m')."""
    return f"{v * 1000:.0f}m"


def _fmt_bytes(v: float) -> str:
    """Bytes as mebibytes ('512Mi')."""
    return f"{v / 1024**2:.0f}Mi"


def _fmt_pct(v: float) -> str:
    """A ratio as a percentage ('12.5%')."""
    return f"{100 * v:.1f}%"


def _fmt_rate(v: float) -> str:
    """A per-second rate to three significant digits."""
    return f"{v:.3g}/s"


def _fmt_ms(v: float) -> str:
    """Milliseconds, rounded."""
    return f"{v:.0f}ms"


def _fmt_num(v: float) -> str:
    """A number to four significant digits."""
    return f"{v:.4g}"


# --------------------------------------------------------------------------- label helpers


def _escape(value: str) -> str:
    """A literal escaped as a regex inside a double-quoted PromQL label matcher."""
    return re.escape(value).replace("\\", "\\\\").replace('"', '\\"')


def pod_name_regex(comp: dict) -> str:
    """Regex (to be fully matched) for the component's pod names by its controller's naming scheme."""
    name = re.escape(comp.get("name") or "")
    kind = comp.get("kind")
    if kind == "StatefulSet":
        rx = f"{name}-[0-9]+"
    elif kind in ("DaemonSet", "Job"):
        rx = f"{name}-[a-z0-9]{{5}}"
    elif kind == "CronJob":
        rx = f"{name}-[0-9]+-[a-z0-9]{{5}}"
    else:
        rx = f"{name}-[a-z0-9]{{6,10}}-[a-z0-9]{{5}}"
    return rx


def pod_regex(comp: dict) -> str:
    """pod_name_regex escaped for a PromQL double-quoted string (Prometheus anchors regex matchers)."""
    return pod_name_regex(comp).replace("\\", "\\\\")


def pod_selector(comp: dict, label: str = "pod") -> str:
    """PromQL label matchers for the component's pods."""
    return f'namespace="{comp.get("namespace")}",{label}=~"{pod_regex(comp)}"'


def service_names(comp: dict) -> list[str]:
    """Names the component is known by in metrics: its own name and its Services' names."""
    names = [comp.get("name") or ""] + [s.get("name") for s in comp.get("services") or [] if s.get("name")]
    return [n for n in dict.fromkeys(names) if n]


def instance_selector(comp: dict) -> str:
    """Matcher for scrape targets addressed by the component's Service names (static targets carry no pod label).

    Matches `name`, `name.ns`, `name.ns.svc...`, each with an optional scheme, port and path.
    """
    names = "|".join(_escape(n) for n in service_names(comp))
    ns = _escape(comp.get("namespace") or "")
    dot = "\\\\."
    return f'instance=~"([a-z]+://)?({names})({dot}{ns}({dot}svc.*)?)?(:[0-9]+)?(/.*)?"'


def rate_window(call: PromCall, series: str) -> str:
    """A rate() window of about four scrape intervals (1m to 5m) for the series selector `series`.

    Scrape intervals differ per job (15s, 1m, ...); a fixed [1m] window holds fewer than two samples of a
    counter scraped every minute, and rate() then returns nothing.
    """
    try:
        found = parse_prom(call(f"max(count_over_time({series}[5m]))"))
    except mcp.McpError:
        raise
    except Exception as exc:  # noqa: BLE001 - fall back to a window that suits common intervals
        logger.debug("scrape interval of %s unknown (%s); using %s", series, exc, DEFAULT_RATE_WINDOW)
        return DEFAULT_RATE_WINDOW
    n = max((p[1] for s in found for p in s["points"]), default=0.0)
    if n < 2:
        return DEFAULT_RATE_WINDOW
    interval = 300.0 / n
    return f"{min(300, max(60, int(math.ceil(4 * interval / 15.0)) * 15))}s"


def detect_span_label(call: PromCall) -> str | None:
    """The label that carries the service name on span metrics ("service_name", else "service"), or None."""
    for label in ("service_name", "service"):
        try:
            series = parse_prom(call(f'count({SPAN_CALLS}{{{label}!=""}})'))
        except mcp.McpError:
            raise
        except Exception as exc:  # noqa: BLE001 - a malformed answer means "not this label"
            logger.debug("span label %s not usable: %s", label, exc)
            continue
        if any(p[1] > 0 for s in series for p in s["points"]):
            return label
    return None


def ensure_span_label(ctx: AgentContext, call: PromCall) -> str | None:
    """The span-metric service label, re-detected while absent (at most every SPAN_RECHECK_S): span metrics
    usually appear only after the first export interval, so an early miss must not stick for the whole run."""
    if ctx.span_label:
        return ctx.span_label
    last = ctx.extra.get("span_label_checked_at")
    if last is not None and time.monotonic() - last < SPAN_RECHECK_S:
        return None
    ctx.extra["span_label_checked_at"] = time.monotonic()
    ctx.span_label = detect_span_label(call)
    return ctx.span_label


def known_span_services(ctx: AgentContext, call: PromCall, label: str) -> list[str]:
    """Service names present on span metrics, refreshed after SPAN_SERVICES_TTL_S (services start emitting at
    different times)."""
    cached = ctx.extra.get("span_services")
    at = ctx.extra.get("span_services_at")
    if cached is None or at is None or time.monotonic() - at > SPAN_SERVICES_TTL_S:
        cached = span_services(call, label)
        ctx.extra["span_services"] = cached
        ctx.extra["span_services_at"] = time.monotonic()
    return cached


def span_services(call: PromCall, label: str) -> list[str]:
    """Service names present in the span metrics under `label`."""
    series = parse_prom(call(f"count by ({label})({SPAN_CALLS})"))
    return sorted({s["labels"].get(label) for s in series if s["labels"].get(label)})


def match_span_services(comp: dict, known: list[str]) -> list[str]:
    """Span-metric service names that belong to the component: exact name/Service matches, else token matches."""
    names = service_names(comp)
    exact = [k for k in known if k in names]
    if exact:
        return exact
    out = []
    for k in known:
        parts = set(re.split(r"[-_.]", k.lower()))
        if any(n.lower() in parts or n.lower() == k.lower() for n in names):
            out.append(k)
    return out or names[:1]


# --------------------------------------------------------------------------- families


def _series_lines(
    call: PromCall, query: str, label_key: str | None, title: str, fmt: Callable[[float], str], max_series: int = 3
) -> tuple[list[str], list[dict]]:
    """(a summary line per series, raw summaries) for the first `max_series` non-empty series of a query, each titled
    with its `label_key` label."""
    series = [s for s in parse_prom(call(query)) if s["points"]]
    lines, raw = [], []
    for s in series[:max_series]:
        summary = summarize_series(s["points"])
        name = s["labels"].get(label_key) if label_key else None
        lines.append(describe_summary(f"{title}[{name}]" if name else title, summary, fmt))
        raw.append({"labels": s["labels"], "summary": summary})
    return lines, raw


def cpu_lines(call: PromCall, comp: dict) -> tuple[list[str], dict]:
    """CPU usage and throttling of the component's containers over the window, summarized."""
    sel = pod_selector(comp) + ',container!="",container!="POD"'
    w = rate_window(call, f"container_cpu_usage_seconds_total{{{sel}}}")
    usage_q = f"(sum by (container)(rate(container_cpu_usage_seconds_total{{{sel}}}[{w}]))){RANGE}"
    throttle_q = (
        f"(sum by (container)(rate(container_cpu_cfs_throttled_periods_total{{{sel}}}[{w}])) / "
        f"sum by (container)(rate(container_cpu_cfs_periods_total{{{sel}}}[{w}]))){RANGE}"
    )
    limits = {c.get("name"): (c.get("limits") or {}).get("cpu") for c in comp.get("containers") or []}
    lines, raw = [], {"usage": [], "throttled": []}
    for s in [s for s in parse_prom(call(usage_q)) if s["points"]][:3]:
        container = s["labels"].get("container")
        summary = summarize_series(s["points"])
        lim = parse_cpu_millis(limits.get(container))
        extra = (
            f"; limit {limits.get(container)} (recent {100 * summary['recent'] * 1000 / lim:.0f}% of it)"
            if lim
            else "; no cpu limit"
        )
        lines.append(describe_summary(f"cpu usage[{container}]", summary, _fmt_cores, extra))
        raw["usage"].append({"container": container, "summary": summary})
    t_lines, raw["throttled"] = _series_lines(call, throttle_q, "container", "cpu throttled periods", _fmt_pct)
    lines += t_lines
    return lines, raw


def memory_lines(call: PromCall, comp: dict) -> tuple[list[str], dict]:
    """Memory working set of the component's containers over the window, against their limits."""
    sel = pod_selector(comp) + ',container!="",container!="POD"'
    ws_q = f"(max by (container)(container_memory_working_set_bytes{{{sel}}})){RANGE}"
    lim_q = f'max by (container)(kube_pod_container_resource_limits{{{pod_selector(comp)},resource="memory"}})'
    limits: dict[str, float] = {}
    for s in parse_prom(call(lim_q)):
        if s["points"]:
            limits[s["labels"].get("container")] = s["points"][-1][1]
    for c in comp.get("containers") or []:
        if c.get("name") not in limits:
            v = parse_memory_bytes((c.get("limits") or {}).get("memory"))
            if v:
                limits[c.get("name")] = v
    lines, raw = [], []
    for s in [s for s in parse_prom(call(ws_q)) if s["points"]][:3]:
        container = s["labels"].get("container")
        summary = summarize_series(s["points"])
        lim = limits.get(container)
        extra = (
            f"; limit {_fmt_bytes(lim)} (max {100 * summary['max'] / lim:.0f}% of it)" if lim else "; no memory limit"
        )
        lines.append(describe_summary(f"memory working set[{container}]", summary, _fmt_bytes, extra))
        raw.append({"container": container, "summary": summary, "limit": lim})
    return lines, {"working_set": raw}


def restarts_lines(call: PromCall, comp: dict) -> tuple[list[str], dict]:
    """Restarts per container over the window (increase(), so a replaced pod's series does not cancel out), with
    the restarts the collector judged warm-up churn on pods that have settled since called out."""
    from clients.jev_agent.overview import settled_pods

    sel = pod_selector(comp)
    w = rate_window(call, f"kube_pod_container_status_restarts_total{{{sel}}}")
    q = f"(sum by (container)(increase(kube_pod_container_status_restarts_total{{{sel}}}[{w}]))){RANGE}"
    total_q = f"sum by (container)(kube_pod_container_status_restarts_total{{{sel}}})"
    totals = {s["labels"].get("container"): s["points"][-1][1] for s in parse_prom(call(total_q)) if s["points"]}
    settled = settled_pods(comp)
    startup = {p.get("name"): int(p.get("restarts") or 0) for p in comp.get("pods") or [] if p.get("name") in settled}
    lines, raw = [], []
    for s in [s for s in parse_prom(call(q)) if s["points"]][:4]:
        container = s["labels"].get("container")
        vals = [max(0.0, v) for _, v in s["points"]]
        rising = [i for i, v in enumerate(vals) if v > 0.5]
        text = f"restarts[{container}]: total {totals.get(container, 0):.0f}"
        if rising:
            text += f"; restarting within the last {WINDOW}, latest ~{int(s['points'][-1][0] - s['points'][rising[-1]][0])}s before the latest sample"
        else:
            text += f"; none in the last {WINDOW}"
        lines.append(text)
        raw.append({"container": container, "total": totals.get(container, 0), "recent": bool(rising)})
    if not lines:
        for container, total in list(totals.items())[:4]:
            lines.append(f"restarts[{container}]: total {total:.0f}")
            raw.append({"container": container, "total": total})
    if startup and sum(startup.values()):
        lines.append(
            f"of these, {sum(startup.values())} were start-up restarts during warm-up on pod(s) "
            f"{', '.join(sorted(startup))[:120]}, ready and running since"
        )
    for metric, what in (
        ("kube_pod_container_status_last_terminated_reason", "last terminated reason (kept until the next crash)"),
        ("kube_pod_container_status_waiting_reason", "waiting reason"),
    ):
        series = parse_prom(call(f"max by (container, reason)({metric}{{{sel}}}) == 1"))
        found = sorted({f"{s['labels'].get('container')}: {s['labels'].get('reason')}" for s in series})
        if found:
            lines.append(f"{what}: " + ", ".join(found[:4]))
    return lines, {"restarts": raw, "startup_restarts": startup}


def rpc_lines(call: PromCall, comp: dict, label: str | None, known: list[str] | None = None) -> tuple[list[str], dict]:
    """Request rate, error ratio and latency of the component from span metrics, as server and as client."""
    if not label:
        return ["no span metrics for any service in Prometheus at this time"], {}
    names = match_span_services(comp, known or [])
    regex = "|".join(_escape(n) for n in names)
    sel = f'{label}=~"{regex}"'
    server = sel + ',span_kind=~"SPAN_KIND_SERVER|SPAN_KIND_CONSUMER"'
    w = rate_window(call, f"{SPAN_CALLS}{{{sel}}}")
    rate_q = f"(sum(rate({SPAN_CALLS}{{{server}}}[{w}]))){RANGE}"
    lines, rate_raw = _series_lines(call, rate_q, None, f"server span rate ({','.join(names)})", _fmt_rate)
    if not lines:
        server = sel
        rate_q = f"(sum(rate({SPAN_CALLS}{{{server}}}[{w}]))){RANGE}"
        lines, rate_raw = _series_lines(call, rate_q, None, f"span rate, all kinds ({','.join(names)})", _fmt_rate)
    err_q = (
        f'((sum(rate({SPAN_CALLS}{{{server},status_code="STATUS_CODE_ERROR"}}[{w}])) or vector(0)) / '
        f"sum(rate({SPAN_CALLS}{{{server}}}[{w}]))){RANGE}"
    )
    err_lines, err_raw = _series_lines(call, err_q, None, "error ratio", _fmt_pct)
    p95_q = f"(histogram_quantile(0.95, sum by (le)(rate({SPAN_BUCKETS}{{{server}}}[{w}])))){RANGE}"
    p95_lines, p95_raw = _series_lines(call, p95_q, None, "p95 latency", _fmt_ms)
    lines += err_lines + p95_lines
    top_q = (
        f'topk(3, sum by (span_name)(increase({SPAN_CALLS}{{{sel},status_code="STATUS_CODE_ERROR"}}[{WINDOW}]))) > 0'
    )
    top = [s for s in parse_prom(call(top_q)) if s["points"]]
    if top:
        lines.append(
            f"spans with errors in the last {WINDOW}: "
            + ", ".join(f"{s['labels'].get('span_name')} x{s['points'][-1][1]:.0f}" for s in top)
        )
    if not lines:
        lines = [f"no span metrics for service name(s) {', '.join(names)} under label {label}"]
    return lines, {"services": names, "rate": rate_raw, "errors": err_raw, "p95": p95_raw}


def probe_lines(call: PromCall, comp: dict) -> tuple[list[str], dict]:
    """Blackbox probe success for the component's Services over the window."""
    names = "|".join(_escape(n) for n in service_names(comp))
    ns = comp.get("namespace")
    q = (
        f'(max by (instance)(probe_success{{instance=~"(.*://)?({names})([.:/].*)?"}} or '
        f'probe_success{{namespace="{ns}",deployment="{comp.get("name")}"}})){RANGE}'
    )
    series = [s for s in parse_prom(call(q)) if s["points"]]
    lines, raw = [], []
    for s in series[:3]:
        vals = [v for _, v in s["points"]]
        failed = sum(1 for v in vals if v < 1)
        inst = s["labels"].get("instance") or "?"
        text = f"blackbox probe {inst}: now {'up' if vals[-1] >= 1 else 'down'}; failed {failed}/{len(vals)} samples"
        if failed:
            first = next(i for i, v in enumerate(vals) if v < 1)
            text += f"; first failure ~{int(s['points'][-1][0] - s['points'][first][0])}s before the latest sample"
        lines.append(text)
        raw.append({"instance": inst, "failed": failed, "samples": len(vals)})
    if not lines:
        lines = [f"no blackbox probe_success series for {', '.join(service_names(comp))}"]
    return lines, {"probes": raw}


def app_lines(call: PromCall, comp: dict) -> tuple[list[str], dict]:
    """Application metrics of the component, top 3 by relative change.

    Series are found by pod label (pod / kubernetes_pod_name) or, for static scrape targets that carry no pod
    label, by an `instance` that addresses one of the component's Services. Infrastructure families
    (container_, kube_, probe_, ...) are excluded.
    """
    names: list[str] = []
    sel = ""
    source = ""
    candidates = [
        ("pod label", pod_selector(comp, "pod")),
        ("pod label", pod_selector(comp, "kubernetes_pod_name")),
        ("scrape target", instance_selector(comp)),
    ]
    for what, cand in candidates:
        q = f'count by (__name__)({{{cand},__name__!~"{_INFRA_METRIC_RE}"}})'
        names = sorted({s["labels"].get("__name__") for s in parse_prom(call(q)) if s["labels"].get("__name__")})
        if names:
            sel, source = cand, what
            break
    names = [n for n in names if not n.endswith("_bucket")]
    if not names:
        return ["no application metrics found on this component's pods or Service scrape targets"], {}
    targets: list[str] = []
    if source == "scrape target":
        q = f'count by (instance, job)({{{sel},__name__!~"{_INFRA_METRIC_RE}"}})'
        targets = sorted({s["labels"].get("instance") for s in parse_prom(call(q)) if s["labels"].get("instance")})
    scored = []
    for name in names[:MAX_APP_CANDIDATES]:
        selected = f"{name}{{{sel}}}"
        counter = name.endswith(("_total", "_count", "_sum"))
        inner = f"rate({selected}[{rate_window(call, selected)}])" if counter else selected
        series = [s for s in parse_prom(call(f"(sum({inner})){RANGE}")) if s["points"]]
        if not series:
            continue
        summary = summarize_series(series[0]["points"])
        if summary.get("baseline") is None:
            continue
        base, recent = summary["baseline"], summary["recent"]
        rel = abs(math.log((abs(recent) + 1e-9) / (abs(base) + 1e-9))) if (base or recent) else 0.0
        scored.append((rel, name, counter, summary))
    scored.sort(key=lambda x: -x[0])
    lines = [
        describe_summary(f"{name}{' rate' if counter else ''}", summary, _fmt_rate if counter else _fmt_num)
        for _, name, counter, summary in scored[:3]
    ]
    limits = []
    for name in [n for n in names if _LIMIT_NAME_RE.search(n) and not n.endswith(("_total", "_count", "_sum"))][:4]:
        found = [s for s in parse_prom(call(f"sum({name}{{{sel}}})")) if s["points"]]
        if found:
            limits.append(f"{name}={_fmt_num(found[0]['points'][-1][1])}")
    if limits:
        lines.append("declared limits (current values): " + ", ".join(limits))
    where = f" (scrape target {', '.join(targets[:2])})" if targets else ""
    lines.append(f"{len(names)} application metric names found{where}: {', '.join(names[:8])}")
    return lines, {
        "source": source,
        "targets": targets,
        "candidates": names,
        "top": [{"name": n, "summary": s} for _, n, _, s in scored[:3]],
    }


# --------------------------------------------------------------------------- tool entry point


def prom_call(timeout: float = 15.0) -> PromCall:
    """A PromQL caller that queries Prometheus through the MCP server."""

    def _call(query: str) -> str:
        return mcp.call_mcp("prometheus", "get_metrics", {"query": query}, timeout=timeout)

    return _call


def _component(ctx: AgentContext, cid: str) -> dict:
    """The snapshot component with id `cid`; KeyError when there is none."""
    from clients.jev_agent.overview import snapshot_parts

    comp = snapshot_parts(ctx.snapshot)[1].get(cid)
    if comp is None:
        raise KeyError(f"unknown component {cid}")
    return comp


def metric_lines(ctx: AgentContext, cid: str, family: str, call: PromCall | None = None) -> ToolResult:
    """Run one metric family for one component. McpError propagates (the tool runner counts MCP failures)."""
    if family not in FAMILIES:
        raise ValueError(f"unknown metric family {family}")
    raw_call = call or ctx.extra.get("prom_call") or prom_call()
    rejected: list[str] = []

    def call(query: str) -> str:
        """The query's result, or '' (noted in `rejected`) when Prometheus rejects the query."""
        # A query Prometheus rejects yields no series; only availability failures propagate as McpError.
        try:
            return raw_call(query)
        except mcp.McpQueryError as exc:
            rejected.append(str(exc))
            return ""

    comp = _component(ctx, cid)
    started = [parse_time(p.get("started")) for p in comp.get("pods") or []]
    started = [t for t in started if t is not None]
    warm_end = (min(started).timestamp() + WARMUP_SECONDS) if started else None
    token = _WARM_END.set(warm_end)
    try:
        lines, raw = _family_lines(ctx, call, comp, family)
    finally:
        _WARM_END.reset(token)
    if not lines:
        lines = [f"no {family} series found for {cid}"]
    elif warm_end is not None and warm_end > time.time() - 15 * 60 and family != "restarts":
        lines.append(
            f"(pods started {int(time.time() - warm_end + WARMUP_SECONDS)}s ago; samples from their first "
            f"{WARMUP_SECONDS}s of warm-up are left out when enough remain)"
        )
    if rejected:
        lines.append(f"{len(rejected)} quer(ies) rejected by Prometheus, first: {rejected[0]}")
    return ToolResult(lines=lines, raw={"family": family, **raw, "rejected": rejected})


def _family_lines(ctx: AgentContext, call: PromCall, comp: dict, family: str) -> tuple[list[str], dict]:
    """(lines, raw) of one metrics family for a component."""
    if family == "cpu":
        lines, raw = cpu_lines(call, comp)
    elif family == "memory":
        lines, raw = memory_lines(call, comp)
    elif family == "restarts":
        lines, raw = restarts_lines(call, comp)
    elif family == "rpc":
        label = ensure_span_label(ctx, call)
        known = known_span_services(ctx, call, label) if label else None
        lines, raw = rpc_lines(call, comp, label, known)
    elif family == "probe":
        lines, raw = probe_lines(call, comp)
    else:
        lines, raw = app_lines(call, comp)
    return lines, raw
