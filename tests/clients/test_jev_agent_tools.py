"""Unit tests for the jev_agent tool layer: overview whitelisting, scores, metrics, tools, runner, cache, lint."""

from __future__ import annotations

import ast
import json
import math
from pathlib import Path

import pytest

from clients.jev_agent import mcp, metrics, overview, tools
from clients.jev_agent.tools import _common, _podexec, backend_probe
from clients.jev_agent.types import (
    CLUSTER_GROUP,
    CLUSTER_GROUPS,
    COMPONENT,
    EDGE,
    MAX_FINDINGS,
    MAX_LINE_CHARS,
    SCOPE,
    AgentContext,
    ToolResult,
    ToolSpec,
)
from clients.jev_diag import collector, investigate, probes

PACKAGE = Path(__file__).resolve().parents[2] / "clients" / "jev_agent"
FORBIDDEN = {
    "local_traffic_policy_gaps",
    "shared_rwo_claims",
    "admission_changes",
    "stuck_terminating",
    "link_stuck_objects",
    "coredns_service_overrides",
    "server_login_rejections",
    "post_process",
}


def make_ctx(snapshot: dict, **kw) -> AgentContext:
    ov = overview.build_overview(snapshot)
    return AgentContext(
        app_info={"app_name": "test"},
        namespaces=list(snapshot["application"]["namespaces"]),
        snapshot=snapshot,
        overview=ov,
        **kw,
    )


def keys_in(obj) -> set[str]:
    out: set[str] = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.add(k)
            out |= keys_in(v)
    elif isinstance(obj, list):
        for v in obj:
            out |= keys_in(v)
    return out


def simple_snapshot() -> dict:
    """Two-component synthetic snapshot: a crash-looping api calling a healthy db."""
    return {
        "application": {"name": "demo", "namespaces": ["demo"]},
        "collected_at": "2026-10-10T00:10:00+00:00",
        "cluster": {"symptom_onset": "2026-10-10T00:05:00+00:00", "recent_changes": []},
        "components": {
            "deployment/api": {
                "kind": "Deployment",
                "name": "api",
                "namespace": "demo",
                "created": "2026-10-10T00:00:00Z",
                "modified": "2026-10-10T00:00:00Z",
                "replicas": {"desired": 2, "ready": 1},
                "containers": [{"name": "api", "image": "demo/api:1", "ports": [8080], "limits": {"memory": "128Mi"}}],
                "pods": [
                    {
                        "name": "api-5d8f7c9b4-abcde",
                        "phase": "Running",
                        "ready": "0/1",
                        "restarts": 7,
                        "container_states": [
                            "api: waiting CrashLoopBackOff: back-off 5m0s",
                            "api: last terminated Error exit=1",
                        ],
                        "ip": "10.0.0.5",
                    },
                    {"name": "api-5d8f7c9b4-fghij", "phase": "Running", "ready": "1/1", "restarts": 0},
                ],
                "services": [
                    {"name": "api", "ports": ["80->8080/TCP"], "ready_endpoints": 1, "not_ready_endpoints": 1}
                ],
                "warning_events": [{"reason": "BackOff", "count": 12, "object": "pod/api-x", "message": "Back-off"}],
                "log_signals": [
                    {"count": 30, "line": "api: dial tcp db:5432: connect refused", "state": "ongoing"},
                    {"count": 5, "line": "api: otlp exporter export failed", "state": "ongoing"},
                ],
                "resource_usage": [{"pod": "api-5d8f7c9b4-abcde", "container": "api", "cpu": "5m", "memory": "120Mi"}],
                "_calls": ["deployment/db"],
                "signals": ["SECRET-SIGNAL-TEXT container api waiting in CrashLoopBackOff"],
                "spec_flags": ["SECRET-SPEC-FLAG"],
                "healthy": False,
            },
            "deployment/db": {
                "kind": "Deployment",
                "name": "db",
                "namespace": "demo",
                "created": "2026-10-10T00:00:00Z",
                "modified": "2026-10-10T00:00:00Z",
                "replicas": {"desired": 1, "ready": 1},
                "containers": [{"name": "postgres", "image": "postgres:16", "ports": [5432]}],
                "pods": [{"name": "db-7f9c6d5b8-zzzzz", "phase": "Running", "ready": "1/1", "restarts": 0}],
                "services": [
                    {"name": "db", "ports": ["5432->5432/TCP"], "ready_endpoints": 1, "not_ready_endpoints": 0}
                ],
                "clients": ["deployment/api"],
                "signals": [],
                "healthy": True,
            },
            "deployment/grafana": {
                "kind": "Deployment",
                "name": "grafana",
                "namespace": "demo",
                "replicas": {"desired": 1, "ready": 0},
                "containers": [{"name": "grafana", "image": "grafana/grafana:10"}],
                "pods": [{"name": "grafana-6b7c8d9f5-qqqqq", "phase": "Running", "ready": "0/1", "restarts": 0}],
                "signals": [],
            },
        },
    }


# --------------------------------------------------------------------------- overview


def test_overview_never_copies_derived_judgments():
    banned_keys = {
        "signals",
        "spec_flags",
        "evidence_kinds",
        "healthy",
        "findings",
        "blocked_input",
        "stalled_consumers",
    }
    for snap in [simple_snapshot()]:
        ov = overview.build_overview(snap)
        assert not (keys_in(ov) & banned_keys)
        texts = [json.dumps(ov), json.dumps(overview.render(ov, 100_000)), json.dumps(overview.render(ov, 1500))]
        leaked_from = []
        for comp in snap["components"].values():
            leaked_from += comp.get("signals") or []
            leaked_from += comp.get("spec_flags") or []
        for finding in snap["cluster"].get("findings") or []:
            leaked_from.append(finding.get("signal"))
        for rule in (snap["cluster"].get("cluster_dns") or {}).get("rules") or []:
            leaked_from.append(rule)
        for text in texts:
            for sig in leaked_from:
                if sig:
                    assert sig not in text, f"derived text leaked: {sig}"


def test_overview_component_fields_are_generic_facts():
    ov = overview.build_overview(simple_snapshot())
    api = ov["components"]["deployment/api"]
    assert api["replicas"] == {"desired": 2, "ready": 1}
    assert api["pods"][0]["waiting"] == ["api: CrashLoopBackOff"]
    assert api["pods"][0]["last_terminated"] == ["api: Error exit=1"]
    assert api["warning_events"]["count"] == 12
    assert api["endpoints"] == [{"service": "api", "ready": 1, "not_ready": 1}]
    assert api["errors"]["ongoing_lines"] == 30  # the telemetry-export line is excluded
    assert api["errors"]["telemetry_export_lines"] == 5
    assert api["depends_on"] == ["deployment/db"]
    assert ov["components"]["deployment/db"]["role"] == "postgresql"
    assert ov["components"]["deployment/grafana"]["role"] == "observability"
    text = json.dumps(overview.render(ov))
    assert "SECRET-SIGNAL-TEXT" not in text and "SECRET-SPEC-FLAG" not in text


def test_component_scores_order_and_neighbors():
    ov = overview.build_overview(simple_snapshot())
    scores = overview.component_scores(ov)
    assert scores["deployment/api"] > scores["deployment/grafana"] > scores["deployment/db"] == 0
    assert overview.neighbors(ov, "deployment/db") == ["deployment/api"]
    assert overview.neighbors(ov, "deployment/api") == ["deployment/db"]
    assert overview.ranked_components(ov)[0] == "deployment/api"


def test_render_respects_budget():
    ov = overview.build_overview(simple_snapshot())
    full = overview.render(ov, 100_000)
    small = overview.render(ov, 300)
    assert collector.estimate_tokens(small) < collector.estimate_tokens(full)
    assert "components_without_abnormal_facts" in small
    assert small["components"], "the most abnormal components stay visible"


# --------------------------------------------------------------------------- metrics


def test_summarize_series_step_and_onset():
    pts = [(1000 + 30 * i, 1.0 + (0.01 if i % 2 else 0)) for i in range(20)] + [
        (1600 + 30 * i, 10.0) for i in range(10)
    ]
    s = metrics.summarize_series(pts)
    assert s["baseline"] == pytest.approx(1.0, abs=0.02)
    assert s["recent"] == 10.0
    assert s["max"] == 10.0
    assert s["change"] == pytest.approx(10.0, rel=0.05)
    assert s["onset_index"] == 20
    assert s["onset_direction"] == "up"
    assert s["onset_s_before_end"] == 270


def test_summarize_series_robust_to_nan_short_and_flat():
    assert metrics.summarize_series([]) == {"n": 0}
    assert metrics.summarize_series([float("nan"), "NaN", None])["n"] == 0
    short = metrics.summarize_series([1.0, 2.0])
    assert short["n"] == 2 and short["onset_index"] is None
    flat = metrics.summarize_series([5.0] * 30)
    assert flat["onset_index"] is None and flat["change"] == 1.0
    spike = metrics.summarize_series([5.0] * 15 + [50.0] + [5.0] * 14)
    assert spike["onset_index"] is None  # a single outlier is not an onset
    zero_base = metrics.summarize_series([0.0] * 10 + [3.0] * 10 + [float("nan")])
    assert zero_base["change"] is None and zero_base["delta"] > 0 and zero_base["onset_index"] == 10


def test_parse_prom_vector_matrix_and_scalar():
    vector = "{'resultType': 'vector', 'result': [{'metric': {'container': 'a'}, 'value': [1700000000.5, '0.25']}]}"
    parsed = metrics.parse_prom(vector)
    assert parsed == [{"labels": {"container": "a"}, "points": [(1700000000.5, 0.25)]}]
    matrix = (
        "{'resultType': 'matrix', 'result': [{'metric': {'pod': 'p'}, 'values': [[1, '1'], [2, 'NaN'], [3, '+Inf'], "
        "[4, '3.5']]}]}"
    )
    assert metrics.parse_prom(matrix)[0]["points"] == [(1.0, 1.0), (4.0, 3.5)]
    assert metrics.parse_prom("{'resultType': 'scalar', 'result': [5, '2']}")[0]["points"] == [(5.0, 2.0)]
    assert metrics.parse_prom("not a literal") == []
    assert metrics.parse_prom('{"resultType": "vector", "result": []}') == []


def test_detect_span_label_and_pod_regex():
    def call(q: str) -> str:
        if 'service_name!=""' in q:
            return "{'resultType': 'vector', 'result': []}"
        return "{'resultType': 'vector', 'result': [{'metric': {}, 'value': [1, '42']}]}"

    assert metrics.detect_span_label(call) == "service"
    assert metrics.detect_span_label(lambda q: "{'resultType': 'vector', 'result': []}") is None
    import re

    rx = metrics.pod_name_regex({"kind": "Deployment", "name": "frontend"})
    assert re.fullmatch(rx, "frontend-6859c775df-2k5wr")
    assert not re.fullmatch(rx, "frontend-proxy-6859c775df-2k5wr")
    assert re.fullmatch(metrics.pod_name_regex({"kind": "StatefulSet", "name": "db"}), "db-0")


def _matrix(series: list[tuple[dict, list[float]]]) -> str:
    result = [
        {"metric": labels, "values": [[1000 + 30 * i, str(v)] for i, v in enumerate(values)]}
        for labels, values in series
    ]
    return str({"resultType": "matrix", "result": result})


def test_metric_lines_cpu_memory_rpc_with_fake_prometheus():
    snap = simple_snapshot()
    ctx = make_ctx(snap)
    queries: list[str] = []

    def call(q: str) -> str:
        queries.append(q)
        if "container_cpu_usage_seconds_total" in q and "throttled" not in q:
            return _matrix([({"container": "api"}, [0.01] * 20 + [0.5] * 10)])
        if "container_memory_working_set_bytes" in q:
            return _matrix([({"container": "api"}, [100 * 1024**2] * 30)])
        if 'count(traces_span_metrics_calls_total{service_name!=""})' in q:
            return "{'resultType': 'vector', 'result': [{'metric': {}, 'value': [1, '3']}]}"
        if q.startswith("count by (service_name)"):
            return str({"resultType": "vector", "result": [{"metric": {"service_name": "api"}, "value": [1, "1"]}]})
        if "traces_span_metrics_calls_total" in q and "STATUS_CODE_ERROR" in q and "topk" not in q:
            return _matrix([({}, [0.0] * 20 + [0.4] * 10)])
        if "traces_span_metrics_calls_total" in q and "topk" not in q:
            return _matrix([({}, [5.0] * 30)])
        return "{'resultType': 'vector', 'result': []}"

    cpu = metrics.metric_lines(ctx, "deployment/api", "cpu", call=call)
    assert cpu.lines[0].startswith("cpu usage[api]: recent 500m (baseline 10m")
    assert "no cpu limit" in cpu.lines[0]
    assert all('pod=~"api-' in q for q in queries if "container_cpu" in q)
    mem = metrics.metric_lines(ctx, "deployment/api", "memory", call=call)
    assert "limit 128Mi" in mem.lines[0] and "no shift from baseline" in mem.lines[0]
    rpc = metrics.metric_lines(ctx, "deployment/api", "rpc", call=call)
    assert ctx.span_label == "service_name"
    assert any(line.startswith("error ratio: recent 40.0%") for line in rpc.lines)
    assert any('service_name=~"api"' in q for q in queries)
    for q in queries:
        assert q.count("(") == q.count(")"), q


def test_rate_window_follows_scrape_interval():
    def vec(n: str):
        return lambda q: str({"resultType": "vector", "result": [{"metric": {}, "value": [1, n]}]})

    assert metrics.rate_window(vec("5"), "x") == "240s"  # scraped every minute: 4 intervals
    assert metrics.rate_window(vec("20"), "x") == "60s"  # every 15s: floor of 1m
    assert metrics.rate_window(vec("2"), "x") == "300s"  # sparse history: capped at 5m
    assert metrics.rate_window(lambda q: "{'resultType': 'vector', 'result': []}", "x") == "4m"


def test_span_label_is_redetected_after_an_early_miss(monkeypatch):
    ctx = make_ctx(simple_snapshot())
    present = {"now": False}

    def call(q: str) -> str:
        if q.startswith("count(traces_span_metrics_calls_total") and present["now"] and "service_name" in q:
            return "{'resultType': 'vector', 'result': [{'metric': {}, 'value': [1, '3']}]}"
        return "{'resultType': 'vector', 'result': []}"

    clock = {"t": 1000.0}
    monkeypatch.setattr(metrics.time, "monotonic", lambda: clock["t"])
    assert metrics.ensure_span_label(ctx, call) is None
    present["now"] = True
    clock["t"] += 5  # within the recheck interval: no new query
    assert metrics.ensure_span_label(ctx, call) is None
    clock["t"] += metrics.SPAN_RECHECK_S
    assert metrics.ensure_span_label(ctx, call) == "service_name"


def test_app_metrics_found_by_service_scrape_target_without_pod_label():
    snap = simple_snapshot()
    ctx = make_ctx(snap)
    queries: list[str] = []

    def call(q: str) -> str:
        queries.append(q)
        if q.startswith("count by (__name__)") and "instance=~" in q:
            names = ["app_queue_capacity", "app_queue_depth", "app_requests_rejected_total"]
            return str(
                {"resultType": "vector", "result": [{"metric": {"__name__": n}, "value": [1, "1"]} for n in names]}
            )
        if q.startswith("count by (instance, job)"):
            return str(
                {
                    "resultType": "vector",
                    "result": [{"metric": {"instance": "api.ns:9091", "job": "j"}, "value": [1, "1"]}],
                }
            )
        if "app_queue_depth" in q:
            return _matrix([({}, [0.0] * 20 + [200.0] * 10)])
        if q.startswith("sum(app_queue_capacity"):
            return str({"resultType": "vector", "result": [{"metric": {}, "value": [1, "256"]}]})
        if "app_queue_capacity" in q:
            return _matrix([({}, [256.0] * 30)])
        if "app_requests_rejected_total" in q and "count_over_time" not in q:
            return _matrix([({}, [0.0] * 30)])
        return "{'resultType': 'vector', 'result': []}"

    res = metrics.metric_lines(ctx, "deployment/api", "app", call=call)
    assert res.lines[0].startswith("app_queue_depth: recent 200")
    assert "scrape target api.ns:9091" in res.lines[-1]
    assert "declared limits (current values): app_queue_capacity=256" in res.lines
    assert any('pod=~"api-' in q for q in queries) and any("instance=~" in q for q in queries)
    for q in queries:
        assert q.count("(") == q.count(")"), q


# --------------------------------------------------------------------------- registry and runner


def test_registry_names_and_kinds():
    expected = {
        "describe": COMPONENT,
        "logs": COMPONENT,
        "service_view": COMPONENT,
        "metric_series_cpu": COMPONENT,
        "metric_series_memory": COMPONENT,
        "metric_series_restarts": COMPONENT,
        "metric_series_rpc": COMPONENT,
        "metric_series_probe": COMPONENT,
        "metric_series_app": COMPONENT,
        "call_edges": COMPONENT,
        "config_and_changes": COMPONENT,
        "cluster_objects": CLUSTER_GROUP,
        "backend_probe": COMPONENT,
        "client_check": EDGE,
        "events": SCOPE,
        "object_view": "object",
        "related_objects": COMPONENT,
    }
    assert {n: s.target_kind for n, s in tools.REGISTRY.items()} == expected
    for name, spec in tools.REGISTRY.items():
        assert spec.name == name and spec.reveals and len(spec.reveals) < 240
        assert spec.uses_mcp == (name.startswith("metric_series_") or name == "call_edges")


def test_candidate_targets():
    ctx = make_ctx(simple_snapshot())
    assert tools.candidate_targets(ctx, tools.REGISTRY["describe"])[0] == "deployment/api"
    assert tools.candidate_targets(ctx, tools.REGISTRY["backend_probe"]) == ["deployment/db"]
    assert "deployment/api->deployment/db" in tools.candidate_targets(ctx, tools.REGISTRY["client_check"])
    assert tools.candidate_targets(ctx, tools.REGISTRY["cluster_objects"]) == list(CLUSTER_GROUPS)
    assert tools.candidate_targets(ctx, tools.REGISTRY["events"]) == ["namespace", "kube-system"]
    ctx.mcp_ok = False
    assert tools.candidate_targets(ctx, tools.REGISTRY["metric_series_cpu"]) == []


def test_run_catches_errors_and_clips_lines(monkeypatch):
    ctx = make_ctx(simple_snapshot())

    def boom(ctx, target):
        raise RuntimeError("kaput")

    def chatty(ctx, target):
        return ToolResult(lines=["x" * 1000] * 20)

    monkeypatch.setitem(tools.REGISTRY, "describe", ToolSpec("describe", COMPONENT, "r", boom))
    res = tools.run(ctx, "describe", "deployment/api")
    assert res.error == "kaput" and res.lines == ["describe(deployment/api) failed: RuntimeError: kaput"]
    monkeypatch.setitem(tools.REGISTRY, "logs", ToolSpec("logs", COMPONENT, "r", chatty))
    res = tools.run(ctx, "logs", "deployment/api")
    assert len(res.lines) == MAX_FINDINGS and all(len(line) <= MAX_LINE_CHARS for line in res.lines)
    res = tools.run(ctx, "service_view", "deployment/nope")
    assert res.error == "not applicable"
    assert tools.run(ctx, "no_such_tool", "x").error == "unknown tool"


def test_mcp_failure_accounting(monkeypatch):
    ctx = make_ctx(simple_snapshot())
    calls = {"n": 0}

    def flaky(query: str) -> str:
        calls["n"] += 1
        raise mcp.McpError("connection refused")

    ctx.extra["prom_call"] = flaky
    first = tools.run(ctx, "metric_series_cpu", "deployment/api")
    assert first.error.startswith("mcp:") and ctx.mcp_failures == 1 and ctx.mcp_ok
    ctx.extra["prom_call"] = lambda q: "{'resultType': 'vector', 'result': []}"
    tools.run(ctx, "metric_series_cpu", "deployment/api")
    assert ctx.mcp_failures == 0 and ctx.mcp_ok
    ctx.extra["prom_call"] = flaky
    tools.run(ctx, "metric_series_memory", "deployment/api")
    tools.run(ctx, "metric_series_memory", "deployment/api")
    assert ctx.mcp_failures == 2 and ctx.mcp_ok is False
    assert tools.run(ctx, "metric_series_cpu", "deployment/api").error == "not applicable"


def test_tool_cache_round_trip_and_replay(tmp_path):
    path = tmp_path / "tools.jsonl.gz"
    ctx = make_ctx(simple_snapshot(), cache=tools.ToolCache(path))
    ctx.extra["prom_call"] = lambda q: "{'resultType': 'vector', 'result': []}"
    live = tools.run(ctx, "metric_series_probe", "deployment/api")
    ctx.cache.record("events", "namespace", ToolResult(lines=["first"]))
    ctx.cache.record("events", "namespace", ToolResult(lines=["second"], raw={"k": 1}))
    assert ctx.cache.lookup("events", "namespace").lines == ["second"]
    ctx.cache.close()

    replay = tools.ToolCache(path, replay=True)
    assert replay.lookup("metric_series_probe", "deployment/api").lines == live.lines
    assert replay.lookup("events", "namespace").lines == ["first"]
    assert replay.lookup("events", "namespace").raw == {"k": 1}
    assert replay.lookup("events", "namespace").lines == ["second"]  # the last one repeats
    rctx = make_ctx(simple_snapshot(), cache=replay)
    assert tools.run(rctx, "metric_series_probe", "deployment/api").lines == live.lines
    assert tools.run(rctx, "logs", "deployment/db").error == "not recorded"


# --------------------------------------------------------------------------- tools with fake kubectl


def fake_kubectl(monkeypatch, responses: dict[str, object]):
    seen: list[str] = []

    def run_kubectl(args, *, timeout=45):
        cmd = " ".join(args)
        seen.append(cmd)
        for key, value in responses.items():
            if cmd.startswith(key):
                if isinstance(value, Exception):
                    raise value
                return value if isinstance(value, str) else json.dumps(value)
        raise RuntimeError(f"unexpected kubectl {cmd}")

    monkeypatch.setattr(collector, "run_kubectl", run_kubectl)
    return seen


def test_events_grouped_newest_first(monkeypatch):
    ctx = make_ctx(simple_snapshot())
    events = {
        "items": [
            {
                "type": "Warning",
                "reason": "BackOff",
                "count": 3,
                "involvedObject": {"kind": "Pod", "name": "api-5d8f7c9b4-abcde"},
                "lastTimestamp": "2026-10-10T00:09:00Z",
                "message": "Back-off restarting failed container",
            },
            {
                "type": "Warning",
                "reason": "BackOff",
                "count": 2,
                "involvedObject": {"kind": "Pod", "name": "api-5d8f7c9b4-fghij"},
                "lastTimestamp": "2026-10-10T00:08:00Z",
                "message": "older",
            },
            {
                "type": "Normal",
                "reason": "Pulled",
                "involvedObject": {"kind": "Pod", "name": "db-7f9c6d5b8-zzzzz"},
                "lastTimestamp": "2026-10-10T00:01:00Z",
                "message": "pulled",
            },
        ]
    }

    def pod(name: str, ready_since: str) -> dict:
        return {
            "metadata": {"name": name, "namespace": "demo"},
            "status": {"conditions": [{"type": "Ready", "status": "True", "lastTransitionTime": ready_since}]},
        }

    # api-...abcde is Ready since 00:08:30, so its 00:09 BackOff is current; api-...fghij is gone;
    # db became Ready after its 00:01 event, which is start-up history.
    pods = {
        "items": [pod("api-5d8f7c9b4-abcde", "2026-10-10T00:08:30Z"), pod("db-7f9c6d5b8-zzzzz", "2026-10-10T00:02:00Z")]
    }
    fake_kubectl(monkeypatch, {"get events -o json -n demo": events, "get pods -o json -n demo": pods})
    res = tools.run(ctx, "events", "namespace")
    assert res.lines[0].startswith("3 events in demo: 2 Warning, 1 Normal")
    assert res.lines[1].startswith("Warning BackOff x3 on 1 Pod(s)") and "Back-off restarting" in res.lines[1]
    assert res.lines[-1].startswith("events of pods no longer present: BackOff x2 on 1 Pod(s)")
    only_api = tools.run(ctx, "events", "deployment/api")
    assert only_api.lines[0].startswith("2 events in demo for deployment/api")
    assert not any("Pulled" in ln for ln in only_api.lines)


def test_service_view_reports_raw_ports_and_matches(monkeypatch):
    ctx = make_ctx(simple_snapshot())
    svc = {
        "items": [
            {
                "metadata": {"name": "api", "namespace": "demo"},
                "spec": {
                    "type": "ClusterIP",
                    "clusterIP": "10.96.0.10",
                    "selector": {"app": "api"},
                    "ports": [{"name": "http", "port": 80, "targetPort": 9999, "protocol": "TCP"}],
                },
            }
        ]
    }
    eps = {
        "items": [
            {
                "metadata": {"name": "api"},
                "subsets": [{"notReadyAddresses": [{"ip": "10.0.0.5"}], "ports": [{"port": 9999}]}],
            }
        ]
    }
    pods = {
        "items": [
            {
                "metadata": {
                    "name": "api-5d8f7c9b4-abcde",
                    "namespace": "demo",
                    "labels": {"app": "api"},
                    "ownerReferences": [{"kind": "ReplicaSet", "name": "api-5d8f7c9b4"}],
                }
            }
        ]
    }
    fake_kubectl(
        monkeypatch,
        {
            "get services -o json --show-managed-fields -n demo": svc,
            "get endpoints -o json -n demo": eps,
            "get pods -o json -n demo": pods,
        },
    )
    res = tools.run(ctx, "service_view", "deployment/api")
    text = "\n".join(res.lines)
    assert "http 80->9999/TCP" in text
    assert "ready 0" in text and "notReady 1" in text
    assert "api-5d8f7c9b4-abcde(deployment/api)" in text
    assert "deployment/api container ports: api: [8080]" in text
    assert res.error is None


def test_describe_redacts_credentials(monkeypatch):
    ctx = make_ctx(simple_snapshot())
    workload = {
        "created": "2026-10-10T00:00:00Z",
        "replicas": 2,
        "dns_policy": "ClusterFirst",
        "containers": [
            {
                "name": "api",
                "image": "demo/api:1",
                "args": ["--port=8080"],
                "env": [
                    {"name": "DB_HOST", "value": "db"},
                    {"name": "DB_PASSWORD", "value": "hunter2"},
                    {"name": "TOKEN", "from": "secret:api.token"},
                ],
                "probes": {"readiness": {"httpGet": {"path": "/health", "port": 8080}, "periodSeconds": 5}},
            }
        ],
        "spec_writers": [{"manager": "kubectl-patch", "op": "Update", "time": "2026-10-10T00:04:00Z"}],
    }
    pods = [
        {
            "name": "api-5d8f7c9b4-abcde",
            "phase": "Running",
            "containers": [
                {"name": "api", "ready": False, "restarts": 7, "state": {"waiting": {"reason": "CrashLoopBackOff"}}}
            ],
        }
    ]
    monkeypatch.setattr(investigate, "workload_detail", lambda kind, name, ns: workload)
    monkeypatch.setattr(investigate, "pod_details", lambda names, ns, **kw: (pods, []))
    fake_kubectl(monkeypatch, {"get pods -o json -n demo": RuntimeError("offline")})
    res = tools.run(ctx, "describe", "deployment/api")
    text = "\n".join(res.lines)
    assert "hunter2" not in text and "DB_PASSWORD=<redacted>" in text and "DB_HOST=db" in text
    assert "waiting CrashLoopBackOff" in text
    assert "readiness httpGet HTTP :8080/health (period=5)" in text
    assert "kubectl-patch Update 60s before symptom onset" in text


def test_config_and_changes_reads_secret_keys_only(monkeypatch):
    snap = simple_snapshot()
    snap["components"]["deployment/api"]["config_refs"] = {"secrets": ["api-creds"], "configmaps": ["api-config"]}
    ctx = make_ctx(snap)
    workload = {
        "metadata": {
            "creationTimestamp": "2026-10-10T00:00:00Z",
            "managedFields": [
                {"manager": "kubectl", "operation": "Update", "time": "2026-10-10T00:00:00Z", "fieldsV1": {}},
                {
                    "manager": "kubectl-patch",
                    "operation": "Update",
                    "time": "2026-10-10T00:04:30Z",
                    "fieldsV1": {"f:spec": {"f:replicas": {}}},
                },
            ],
        }
    }
    secret = {"metadata": {"creationTimestamp": "2026-10-10T00:00:00Z"}, "data": {"password": "c3VwZXJzZWNyZXQ="}}
    fake_kubectl(
        monkeypatch,
        {
            "get deployment api -n demo": workload,
            "get replicasets": {"items": []},
            "get secret api-creds": secret,
        },
    )
    monkeypatch.setattr(
        investigate,
        "configmap_detail",
        lambda names, ns: {"api-config": {"last_update": None, "data": {"LOG": "debug"}}},
    )
    res = tools.run(ctx, "config_and_changes", "deployment/api")
    text = "\n".join(res.lines)
    assert "c3VwZXJzZWNyZXQ" not in text and "supersecret" not in text
    assert "keys ['password'] (values not read)" in text
    assert "fields written 30s before symptom onset: spec.replicas" in text
    assert "ConfigMap api-config" in text and "LOG=debug" in text


def test_cluster_objects_dns(monkeypatch):
    ctx = make_ctx(simple_snapshot())
    corefile = ".:53 {\n    errors\n    forward . /etc/resolv.conf\n    template ANY ANY x.demo.svc.cluster.local {\n      rcode NXDOMAIN\n    }\n}\n"
    cm = {"metadata": {"creationTimestamp": "2026-09-01T00:00:00Z"}, "data": {"Corefile": corefile}}
    pods = {
        "items": [
            {
                "metadata": {"name": "coredns-1"},
                "status": {"phase": "Running", "containerStatuses": [{"ready": True, "restartCount": 0}]},
            }
        ]
    }
    eps = {"subsets": [{"addresses": [{"ip": "10.0.0.2"}]}]}
    fake_kubectl(
        monkeypatch,
        {
            "get configmap coredns": cm,
            "get pods -n kube-system": pods,
            "get endpoints kube-dns": eps,
        },
    )
    res = tools.run(ctx, "cluster_objects", "dns")
    text = "\n".join(res.lines)
    assert "server blocks ['.:53']" in text
    assert "Corefile block: template ANY ANY x.demo.svc.cluster.local" in text
    assert "kube-dns pods (1): coredns-1 Running ready 1/1 restarts 0" in text
    assert "ready 1, notReady 0" in text
    assert tools.run(ctx, "cluster_objects", "bogus").error == "not applicable"


def test_cluster_objects_admission_names_the_configuration_and_the_pods_it_takes(monkeypatch):
    ctx = make_ctx(simple_snapshot())

    def config(name, webhook, resources, object_selector=None):
        return {
            "kind": "MutatingWebhookConfiguration",
            "metadata": {"name": name, "creationTimestamp": "2026-10-10T00:04:52Z"},
            "webhooks": [
                {
                    "name": webhook,
                    "failurePolicy": "Fail",
                    "timeoutSeconds": 5,
                    "clientConfig": {"service": {"namespace": "platform", "name": "policy-controller", "port": 443}},
                    "rules": [{"apiGroups": [""], "operations": ["CREATE"], "resources": resources}],
                    "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "demo"}},
                    "objectSelector": object_selector,
                }
            ],
        }

    def pod(name, rs, labels):
        owner = [{"kind": "ReplicaSet", "name": rs}]
        return {"metadata": {"name": name, "namespace": "demo", "labels": labels, "ownerReferences": owner}}

    fake_kubectl(
        monkeypatch,
        {
            "get mutatingwebhookconfigurations": {
                "items": [
                    config("policy-mutating-webhook-configuration", "mutation.policy.example", ["pods"]),
                    config("sidecar-injector", "inject.mesh.example", ["pods"], {"matchLabels": {"mesh": "on"}}),
                    config("api-defaults", "defaults.example", ["pods"], {"matchLabels": {"app": "api"}}),
                    config("cert-webhook", "webhook.cert.example", ["certificates"]),
                ]
            },
            "get validatingwebhookconfigurations": {"items": []},
            "get endpoints policy-controller -n platform": {"subsets": [{"addresses": [{"ip": "10.0.0.9"}]}]},
            "get namespace demo": RuntimeError("forbidden"),
            "get pods -o json -n demo": {
                "items": [
                    pod("api-5d8f7c9b4-abcde", "api-5d8f7c9b4", {"app": "api"}),
                    pod("db-7f9c6d5b8-zzzzz", "db-7f9c6d5b8", {"app": "db"}),
                ]
            },
        },
    )
    policy, sidecar, defaults, cert = tools.run(ctx, "cluster_objects", "admission").lines
    # the configuration is the object to act on; the name of its webhook shares nothing with it
    assert policy.startswith(
        "MutatingWebhookConfiguration policy-mutating-webhook-configuration (created 8s before symptom onset): "
        "intercepts the pods of every workload (2) through webhook mutation.policy.example; rules ['CREATE pods']"
    )
    assert policy.endswith("backend platform/policy-controller:443 with 1 ready endpoint(s)")
    assert "intercepts none of the application's pods through webhook inject.mesh.example" in sidecar
    assert "intercepts the pods of deployment/api (1 of 2 workloads)" in defaults
    assert "intercepts no pods through webhook webhook.cert.example" in cert


def test_client_check_reports_resolution_and_connect(monkeypatch):
    ctx = make_ctx(simple_snapshot())
    ctx.snapshot["components"]["deployment/api"]["pods"][0]["ready"] = "1/1"
    fake_kubectl(
        monkeypatch, {"get service db -n demo": {"spec": {"clusterIP": "10.96.1.1", "ports": [{"port": 5432}]}}}
    )
    commands = []

    def run_in_pod(ns, pod, container, command):
        commands.append(command)
        if command[0] == "getent":
            return 0, "10.96.1.1       db.demo.svc.cluster.local"
        if command[2] == "bash":
            return 127, 'exec: "bash": executable file not found in $PATH'
        if command[2] == "sh":
            return 1, "sh: connect: Connection refused"
        return 0, ""

    monkeypatch.setattr(probes, "run_in_pod", run_in_pod)
    res = tools.run(ctx, "client_check", "deployment/api->deployment/db")
    assert res.lines == [
        "from api-5d8f7c9b4-abcde: db.demo.svc.cluster.local resolves to 10.96.1.1 (getent); Service ClusterIP "
        "10.96.1.1; TCP 10.96.1.1:5432 -> refused"
    ]
    assert tools.run(ctx, "client_check", "deployment/api").error == "not applicable"


def test_backend_probe_kafka_reports_stalled_partition(monkeypatch):
    snap = simple_snapshot()
    snap["components"]["deployment/kafka"] = {
        "kind": "Deployment",
        "name": "kafka",
        "namespace": "demo",
        "replicas": {"desired": 1, "ready": 1},
        "containers": [{"name": "kafka", "image": "apache/kafka:3.7", "ports": [9092]}],
        "pods": [{"name": "kafka-6fb5bb8f77-2l7tw", "phase": "Running", "ready": "1/1", "ip": "10.0.0.9"}],
        "services": [{"name": "kafka", "ports": ["9092->9092/TCP"]}],
        "signals": [],
    }
    ctx = make_ctx(snap)
    ctx.extra["broker_interval"] = 15
    monkeypatch.setattr(backend_probe.time, "sleep", lambda s: None)
    row = {
        "GROUP": "g",
        "TOPIC": "orders",
        "PARTITION": "0",
        "CURRENT-OFFSET": "10",
        "CONSUMER-ID": "c1",
        "HOST": "/10.0.0.5",
    }
    samples = iter([[{**row, "LOG-END-OFFSET": "20", "LAG": "10"}], [{**row, "LOG-END-OFFSET": "30", "LAG": "20"}]])
    calls = []

    def sample(ns, pod, container, bootstrap):
        calls.append(bootstrap)
        return "kafka-consumer-groups.sh", next(samples)

    monkeypatch.setattr(backend_probe, "_kafka_sample_from", sample)
    peeks = []

    def fake_exec(ns, pod, container, cmd):
        peeks.append(cmd)
        return (
            0,
            'Partition:0\tOffset:10\t{bad json\nPartition:0\tOffset:11\t{"id": 2}\nProcessed a total of 2 messages',
        )

    monkeypatch.setattr(_podexec, "_exec", fake_exec)
    res = tools.run(ctx, "backend_probe", "deployment/kafka")
    text = "\n".join(res.lines)
    assert calls == ["kafka.demo.svc.cluster.local:9092"] * 2  # always the broker Service, never localhost
    # the record at the stalled offset is read with a bounded JVM and no consumer group
    assert peeks and peeks[0][:2] == ["env", "KAFKA_HEAP_OPTS=-Xms32m -Xmx64m"]
    assert "kafka-console-consumer.sh" in peeks[0] and "--group" not in peeks[0]
    assert peeks[0][peeks[0].index("--offset") + 1] == "10"
    assert "starting at the stalled offset 10" in text and "{bad json" in text and "Processed a total" not in text
    assert "committed offset 10 unchanged over 15s while log end moved 20->30; consumer deployment/api" in text
    assert "lag 20" in text
    assert "blocked_input" not in snap["components"]["deployment/api"]  # the snapshot is never mutated
    assert not snap["components"]["deployment/api"]["signals"][1:]


def test_parse_py_literal_and_error_strings(monkeypatch):
    assert mcp.parse_py_literal("{'a': [1, 'x']}") == {"a": [1, "x"]}
    assert mcp.parse_py_literal('{"a": null}') == {"a": None}
    assert mcp.parse_py_literal("None") is None
    assert mcp.parse_py_literal("garbage {") is None
    monkeypatch.setattr(mcp, "_run_sync", lambda factory, timeout: "[prom_mcp] Error querying get_metrics: boom")
    with pytest.raises(mcp.McpError):
        mcp.call_mcp("prometheus", "get_metrics", {"query": "up"})
    monkeypatch.setattr(mcp, "_run_sync", lambda factory, timeout: "{'resultType': 'vector', 'result': []}")
    assert mcp.preflight() is True

    def fail(factory, timeout):
        raise OSError("refused")

    monkeypatch.setattr(mcp, "_run_sync", fail)
    assert mcp.preflight() is False


def test_call_edges_with_fake_jaeger(monkeypatch):
    ctx = make_ctx(simple_snapshot())
    graph = str(
        {"data": [{"parent": "api", "child": "db", "callCount": 120}, {"parent": "x", "child": "y", "callCount": 5}]}
    )
    monkeypatch.setattr(mcp, "call_mcp", lambda server, tool, args, timeout=15.0: graph)
    ctx.span_label = "service_name"

    def call(q: str) -> str:
        if "STATUS_CODE_ERROR" in q:
            return str({"resultType": "vector", "result": [{"metric": {"service_name": "db"}, "value": [1, "30"]}]})
        return str({"resultType": "vector", "result": [{"metric": {"service_name": "db"}, "value": [1, "120"]}]})

    ctx.extra["prom_call"] = call
    res = tools.run(ctx, "call_edges", "deployment/api")
    assert res.lines == ["api -> db: 120 calls/15m; db spans 25% errors (120 spans)"]


# --------------------------------------------------------------------------- genericity lint


def test_genericity_lint_no_fault_specific_checks():
    offenders = []
    for path in sorted(PACKAGE.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Name):
                names.append(node.id)
            elif isinstance(node, ast.Attribute):
                names.append(node.attr)
            elif isinstance(node, ast.alias):
                names.append(node.name.rsplit(".", 1)[-1])
                if node.asname:
                    names.append(node.asname)
            elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                names.append(node.name)
            offenders += [f"{path.name}:{getattr(node, 'lineno', '?')} {n}" for n in names if n in FORBIDDEN]
    assert not offenders, offenders
    assert math.isfinite(1.0)


def test_rejected_queries_do_not_count_as_mcp_outage(monkeypatch):
    ctx = make_ctx(simple_snapshot())
    monkeypatch.setattr(
        mcp,
        "_run_sync",
        lambda factory, timeout: "[prom_mcp] Error querying get_metrics: 400 Client Error: Bad Request",
    )
    with pytest.raises(mcp.McpQueryError):
        mcp.call_mcp("prometheus", "get_metrics", {"query": "bad("})
    ctx.extra["prom_call"] = lambda q: mcp.call_mcp("prometheus", "get_metrics", {"query": q})
    for _ in range(3):
        res = tools.run(ctx, "metric_series_memory", "deployment/api")
    assert ctx.mcp_ok and ctx.mcp_failures == 0
    assert any("rejected by Prometheus" in line for line in res.lines)


def test_backend_probe_fills_port_without_format(monkeypatch):
    snap = simple_snapshot()
    snap["components"]["deployment/mongo"] = {
        "kind": "Deployment",
        "name": "mongodb-geo",
        "namespace": "demo",
        "replicas": {"desired": 1, "ready": 1},
        "containers": [{"name": "mongo", "image": "mongo:4.4", "ports": [27017]}],
        "pods": [{"name": "mongodb-geo-5ff578bcb8-zm44t", "phase": "Running", "ready": "1/1"}],
    }
    ctx = make_ctx(snap)
    seen = []

    def run_in_pod(ns, pod, container, command):
        seen.append(command)
        return 0, "1"

    monkeypatch.setattr(probes, "run_in_pod", run_in_pod)
    res = tools.run(ctx, "backend_probe", "deployment/mongo")
    assert seen[0][:4] == ["mongo", "--quiet", "--port", "27017"] and "{ping:1}" in seen[0][-1]
    assert res.lines[0].endswith("the server answers a ping")


def test_logs_lists_signatures_and_fetch_failures(monkeypatch):
    ctx = make_ctx(simple_snapshot())
    fake_kubectl(monkeypatch, {"get pods -o json -n demo": RuntimeError("offline")})
    monkeypatch.setattr(investigate, "pod_details", lambda names, ns, **kw: ([], []))
    detail = {
        "total_lines": 300,
        "error_lines": 31,
        "error_lines_still_occurring": 30,
        "error_lines_stopped": 1,
        "error_samples": [
            {
                "count": 30,
                "line": "api: dial tcp db:5432: connect refused",
                "state": "ongoing",
                "first_seen_seconds_ago": 90,
                "last_seen_seconds_ago": 1,
                "median_gap_seconds": 3.0,
            },
            {"count": 1, "line": "[logs unavailable for api-x: timed out]", "state": "stopped"},
        ],
        "previous_container_logs": {"api-x/api": ["starting", "panic: boom"]},
    }
    monkeypatch.setattr(investigate, "log_detail", lambda names, ns, pods, **_: detail)
    res = tools.run(ctx, "logs", "deployment/api")
    assert res.lines[0].startswith("read 300 lines from 2 pod(s): 31 error-like (still occurring 30, stopped 1)")
    assert res.lines[1] == "log fetch failed: api-x: timed out"
    assert res.lines[2].startswith("ongoing x30 (first 90s ago, last 1s ago, every ~3.0s): api: dial tcp")
    assert res.lines[3] == "previous run of api-x/api ended with: starting | panic: boom"


def test_exec_transport_failures_are_tool_errors_not_evidence(monkeypatch):
    """A replay miss or a forbidden exec must not read as 'DNS failed' or 'connection refused'."""
    import pytest

    from clients.jev_agent import tools as tools_mod

    for text in ("not in the replay bundle: kubectl exec ...", 'Error from server (Forbidden): pods "x" is forbidden'):
        monkeypatch.setattr(probes, "run_in_pod", lambda *a, _t=text, **k: (1, _t))
        with pytest.raises(tools_mod.ExecUnavailable):
            tools_mod._resolve("ns", "pod", None, "svc.ns.svc.cluster.local")
    # a real negative answer from inside the container stays evidence
    monkeypatch.setattr(probes, "run_in_pod", lambda *a, **k: (2, ""))
    assert tools_mod._resolve("ns", "pod", None, "svc.ns.svc.cluster.local") == (None, "getent rc=2")


def test_overview_shared_service_and_deploy_time_revision():
    from types import SimpleNamespace

    from clients.jev_agent import overview as ov

    comps = {
        "deployment/frontend": {
            "kind": "Deployment",
            "name": "frontend",
            "namespace": "hr",
            "created": "2026-10-10T06:31:06Z",
            "replicas": {"desired": 1, "ready": 1},
            "pods": [],
            "services": [{"name": "frontend", "ready_endpoints": 2}],
        },
        "deployment/search": {
            "kind": "Deployment",
            "name": "search",
            "namespace": "hr",
            "created": "2026-10-10T06:31:06Z",
            "replicas": {"desired": 1, "ready": 1},
            "pods": [],
            "services": [{"name": "frontend", "ready_endpoints": 2}, {"name": "search", "ready_endpoints": 1}],
        },
        "deployment/mongodb-geo": {
            "kind": "Deployment",
            "name": "mongodb-geo",
            "namespace": "hr",
            "created": "2026-10-10T06:31:00Z",
            "replicas": {"desired": 1, "ready": 1},
            "pods": [],
            "template_changes": {"changed_at": "2026-10-10T06:31:04Z", "changes": ["volume failure-script added"]},
        },
    }
    cluster = {"recent_changes": [{"object": "service/hr/frontend", "fields": ["spec.selector"], "seconds_ago": 130}]}
    snap = SimpleNamespace(
        app={"name": "hr", "namespaces": ["hr"]}, components=comps, cluster=cluster, collected_at="2026-10-10T06:33:30Z"
    )
    o = ov.build_overview(snap)
    assert o["cluster"]["services_selecting_multiple_workloads"] == [
        {"service": "hr/frontend", "selects": ["deployment/frontend", "deployment/search"]}
    ]
    assert o["components"]["deployment/search"]["shared_service"] == ["frontend also selects deployment/frontend"]
    assert o["cluster"]["recent_changes"][0]["selects"] == ["deployment/frontend", "deployment/search"]
    geo = o["components"]["deployment/mongodb-geo"]
    assert geo.get("deploy_time_revision") and "template_changes" not in geo
    scores = ov.component_scores(o)
    assert scores["deployment/search"] > scores["deployment/mongodb-geo"]
    assert "shared_service" in str(ov.render(o))


def test_observability_role_ignores_image_substrings():
    from clients.jev_agent.overview import is_observability

    assert not is_observability({"name": "frontend", "containers": [{"image": "ghcr.io/sregym/hotel-reservation:1"}]})
    assert is_observability({"name": "otel-collector-agent", "containers": []})


def test_client_check_falls_back_to_another_ready_pod(monkeypatch):
    import copy

    snap = simple_snapshot()
    api = snap["components"]["deployment/api"]
    api["pods"][0]["ready"] = "1/1"
    other = copy.deepcopy(api)
    other["name"] = "web"
    other["pods"][0]["name"] = "web-1"
    snap["components"]["deployment/web"] = other
    ctx = make_ctx(snap)
    fake_kubectl(
        monkeypatch, {"get service db -n demo": {"spec": {"clusterIP": "10.96.1.1", "ports": [{"port": 5432}]}}}
    )

    def run_in_pod(ns, pod, container, command):
        if pod != "web-1":
            return 1, 'error: Internal error occurred: unable to upgrade connection: container not found ("api")'
        if command[0] == "getent":
            return 0, "10.96.1.1       db.demo.svc.cluster.local"
        return 0, ""

    monkeypatch.setattr(probes, "run_in_pod", run_in_pod)
    res = tools.run(ctx, "client_check", "deployment/api->deployment/db")
    assert res.error is None
    assert res.lines[0].startswith("deployment/api cannot run commands now; tested from deployment/web")
    assert "from web-1:" in res.lines[1] and "-> ok" in res.lines[1]


def test_logs_name_dependencies_mentioned_in_error_lines():
    from types import SimpleNamespace

    comps = {
        "deployment/cart": {
            "kind": "Deployment",
            "name": "cart",
            "namespace": "shop",
            "containers": [{"image": "cart:1"}],
        },
        "deployment/valkey-cart": {
            "kind": "Deployment",
            "name": "valkey-cart",
            "namespace": "shop",
            "containers": [{"image": "valkey/valkey:8"}],
        },
    }
    ctx = AgentContext(app_info={}, namespaces=["shop"], snapshot=SimpleNamespace(components=comps), overview={})
    assert tools._components_named(ctx, "deployment/cart", ["Wasn't able to connect to redis"]) == [
        "deployment/valkey-cart"
    ]
    assert tools._components_named(ctx, "deployment/cart", ["cart started"]) == []


def test_telemetry_filter_does_not_swallow_hotel_reservation_errors():
    from clients.jev_diag.derive import TELEMETRY_RE

    assert not TELEMETRY_RE.search("hotel-reserv-search: WRN downstream request failed dependency=rate")
    assert TELEMETRY_RE.search("otel-collector: export failed")
    assert TELEMETRY_RE.search("traces export: context deadline exceeded")


def test_logs_fetch_last_words_of_a_crashed_container(monkeypatch):
    from types import SimpleNamespace

    comps = {
        "deployment/cart": {
            "kind": "Deployment",
            "name": "cart",
            "namespace": "shop",
            "containers": [{"image": "cart:1"}],
        },
        "deployment/valkey-cart": {
            "kind": "Deployment",
            "name": "valkey-cart",
            "namespace": "shop",
            "containers": [{"image": "valkey/valkey:8"}],
        },
    }
    ctx = AgentContext(app_info={}, namespaces=["shop"], snapshot=SimpleNamespace(components=comps), overview={})
    pods = [
        {
            "name": "cart-1",
            "containers": [{"name": "cart", "last_state": {"terminated": {"exitCode": 139, "reason": "Error"}}}],
        }
    ]
    monkeypatch.setattr(_common, "_current_pod_names", lambda ctx, comp: ["cart-1"])
    monkeypatch.setattr(investigate, "pod_details", lambda names, ns, **kw: (pods, []))
    monkeypatch.setattr(
        investigate,
        "log_detail",
        lambda names, ns, p, **_: {"total_lines": 1, "tail": ["valkey-cart (10.0.0.1:6379) open"]},
    )
    monkeypatch.setattr(
        collector,
        "run_kubectl",
        lambda args,
        timeout=0: "starting\nUnhandled exception. System.ApplicationException: Wasn't able to connect to redis\n",
    )
    res = tools.run_logs(ctx, "deployment/cart")
    text = "\n".join(res.lines)
    assert "previous run of cart-1/cart ended Error exit=139" in text and "connect to redis" in text
    assert "error lines name other components: deployment/valkey-cart" in text


def test_warmup_crash_is_reported_but_does_not_name_components(monkeypatch):
    from datetime import timedelta
    from types import SimpleNamespace

    from clients.jev_diag.timeutil import now

    def iso(seconds_ago: float) -> str:
        return (now() - timedelta(seconds=seconds_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")

    comps = {
        "deployment/geo": {"kind": "Deployment", "name": "geo", "namespace": "ns", "containers": [{"image": "g:1"}]},
        "deployment/mongodb-geo": {
            "kind": "Deployment",
            "name": "mongodb-geo",
            "namespace": "ns",
            "containers": [{"image": "mongo:4"}],
        },
    }
    ctx = AgentContext(app_info={}, namespaces=["ns"], snapshot=SimpleNamespace(components=comps), overview={})

    def pod(finished_after_start: float, running_for: float) -> dict:
        return {
            "name": "geo-1",
            "started": iso(300),
            "containers": [
                {
                    "name": "geo",
                    "ready": True,
                    "state": {"running": {"startedAt": iso(running_for)}},
                    "last_state": {
                        "terminated": {"exitCode": 2, "reason": "Error", "finishedAt": iso(300 - finished_after_start)}
                    },
                }
            ],
        }

    monkeypatch.setattr(_common, "_current_pod_names", lambda ctx, comp: ["geo-1"])
    monkeypatch.setattr(investigate, "log_detail", lambda names, ns, p, **_: {"total_lines": 1, "tail": ["ok"]})
    monkeypatch.setattr(collector, "run_kubectl", lambda args, timeout=0: "panic: no reachable servers mongodb-geo\n")

    monkeypatch.setattr(investigate, "pod_details", lambda names, ns, **kw: ([pod(10, 280)], []))
    text = "\n".join(tools.run_logs(ctx, "deployment/geo").lines)
    assert (
        "start-up crash, since recovered: previous run of geo-1/geo ended Error exit=2 10s after the pod started"
        in text
    )
    assert "name other components" not in text

    # A crash well after warm-up still counts, and its last words still name the dependency.
    monkeypatch.setattr(investigate, "pod_details", lambda names, ns, **kw: ([pod(250, 40)], []))
    text = "\n".join(tools.run_logs(ctx, "deployment/geo").lines)
    assert "previous run of geo-1/geo ended Error exit=2; its last error lines" in text
    assert "error lines name other components: deployment/mongodb-geo" in text


def test_stopped_errors_do_not_name_components(monkeypatch):
    from types import SimpleNamespace

    comps = {
        "deployment/frontend": {
            "kind": "Deployment",
            "name": "frontend",
            "namespace": "hr",
            "containers": [{"image": "x"}],
        },
        "deployment/consul": {
            "kind": "Deployment",
            "name": "consul",
            "namespace": "hr",
            "containers": [{"image": "consul"}],
        },
    }
    ctx = AgentContext(app_info={}, namespaces=["hr"], snapshot=SimpleNamespace(components=comps), overview={})
    monkeypatch.setattr(_common, "_current_pod_names", lambda ctx, comp: ["frontend-1"])
    monkeypatch.setattr(
        investigate, "pod_details", lambda names, ns, **kw: ([{"name": "frontend-1", "containers": []}], [])
    )
    detail = {"total_lines": 5, "error_samples": [{"state": "stopped", "count": 6, "line": "dial consul:8500 refused"}]}
    monkeypatch.setattr(investigate, "log_detail", lambda names, ns, p, **_: detail)
    assert not any("name other components" in ln for ln in tools.run_logs(ctx, "deployment/frontend").lines)
    detail["error_samples"][0]["state"] = "ongoing"
    assert any("deployment/consul" in ln for ln in tools.run_logs(ctx, "deployment/frontend").lines)


# --------------------------------------------------------------------------- history is not a lead


def test_named_targets_skip_history_lines_and_match_whole_ids():
    from clients.jev_agent.notebook import Notebook, lead_lines, mentions_id
    from clients.jev_agent.policy import named_targets

    assert mentions_id("deployment/frontend", "calls deployment/frontend: refused")
    assert not mentions_id("deployment/frontend", "calls deployment/frontend-proxy: refused")
    assert lead_lines(["stopped x1 (first 200s ago): db down", "ongoing x3: db down"]) == ["ongoing x3: db down"]

    from types import SimpleNamespace

    comps = {
        "deployment/api": {"kind": "Deployment", "name": "api", "namespace": "ns"},
        "deployment/db": {"kind": "Deployment", "name": "db", "namespace": "ns"},
        "deployment/cache": {"kind": "Deployment", "name": "cache", "namespace": "ns"},
    }
    ctx = AgentContext(app_info={}, namespaces=["ns"], snapshot=SimpleNamespace(components=comps), overview={})
    nb = Notebook({}, {})
    nb.add(
        1,
        "logs",
        "deployment/api",
        ToolResult(
            lines=[
                "start-up crash, since recovered: previous run of api-1/api ended Error exit=2; its last error "
                "lines: dial deployment/db: refused",
                "error lines name other components: deployment/cache",
            ]
        ),
    )
    assert named_targets(ctx, nb) == {"deployment/cache"}


def test_settled_pod_restarts_do_not_score():
    from clients.jev_agent import overview as ov

    pod = {
        "name": "geo-1",
        "phase": "Running",
        "ready": "1/1",
        "restarts": 1,
        "container_states": ["geo: last terminated Error exit=2"],
    }
    comp = {
        "startup_history": ["pod geo-1: container geo restarted 1 times during warm-up, then settled (ready since)"]
    }
    assert ov.settled_pods(comp) == {"geo-1"}
    settled = ov._pod_view(pod, settled=True)
    live = ov._pod_view(pod)
    assert settled["restarts"] == 0 and settled["startup_restarts"] == 1 and "last_terminated" not in settled
    assert live["restarts"] == 1 and live["last_terminated"]
    base = {"replicas": {"desired": 1, "ready": 1}}
    scores = ov.component_scores({"components": {"a": {**base, "pods": [settled]}, "b": {**base, "pods": [live]}}})
    assert scores["a"] < scores["b"]


def test_metric_points_from_warmup_are_left_out():
    text = str(
        {"resultType": "matrix", "result": [{"metric": {}, "values": [[1000 + 30 * i, "1"] for i in range(20)]}]}
    )
    token = metrics._WARM_END.set(1000 + 30 * 8)
    try:
        pts = metrics.parse_prom(text)[0]["points"]
    finally:
        metrics._WARM_END.reset(token)
    assert pts[0][0] == 1240 and len(pts) == 12
    token = metrics._WARM_END.set(1000 + 30 * 17)  # too few would remain: keep everything
    try:
        assert len(metrics.parse_prom(text)[0]["points"]) == 20
    finally:
        metrics._WARM_END.reset(token)


def test_written_tells_creation_from_a_later_write():
    ctx = make_ctx(simple_snapshot())
    assert tools._written(ctx, "2026-10-10T00:00:00Z", "2026-10-10T00:00:03Z").endswith("; not written since")
    later = tools._written(ctx, "2026-10-10T00:00:00Z", "2026-10-10T00:05:00Z")
    assert "written again" in later and "(300s after creation)" in later


def test_admission_drift_reports_pod_changes_but_not_kubernetes_defaulting(monkeypatch):
    comp = {"kind": "Deployment", "name": "gw", "namespace": "ns"}
    tpl = {"containers": [{"name": "gw", "image": "nginx:1", "resources": {"limits": {"cpu": "1"}}}]}
    pod = {
        "containers": [
            # requests.cpu = limits.cpu is Kubernetes defaulting; limits.memory was added at admission
            {
                "name": "gw",
                "image": "nginx:1",
                "resources": {"limits": {"cpu": "1", "memory": "256Mi"}, "requests": {"cpu": "1"}},
            },
            {"name": "proxy", "image": "mesh/proxy:2"},
        ]
    }

    def fake_get_json(args):
        return {"spec": {"template": {"spec": tpl}}} if args[1] == "deployment" else {"spec": pod}

    monkeypatch.setattr(_common, "_get_json", fake_get_json)
    text = tools._admission_drift(comp, "gw-1")
    assert text.startswith("pod gw-1 differs from its Deployment template (changed when the pod was admitted")
    assert "container gw limits.memory unset -> 256Mi" in text and "container proxy added" in text
    assert "requests.cpu" not in text
    tpl["containers"][0]["resources"]["limits"]["memory"] = "256Mi"
    pod["containers"] = pod["containers"][:1]
    assert tools._admission_drift(comp, "gw-1") is None


def test_stale_env_refs_reports_config_written_after_container_start(monkeypatch):
    ctx = make_ctx(simple_snapshot())
    comp = {"kind": "Deployment", "name": "api", "namespace": "ns"}
    workload = {
        "spec": {
            "template": {
                "spec": {
                    "containers": [
                        {
                            "name": "api",
                            "env": [{"name": "PW", "valueFrom": {"secretKeyRef": {"name": "db-creds", "key": "pw"}}}],
                        }
                    ]
                }
            }
        }
    }
    pods = [
        {"name": "api-1", "containers": [{"name": "api", "state": {"running": {"startedAt": "2026-10-10T00:00:00Z"}}}]}
    ]
    monkeypatch.setattr(_common, "_current_pod_names", lambda ctx, comp: ["api-1"])
    monkeypatch.setattr(investigate, "pod_details", lambda names, ns, **kw: (pods, []))
    meta = {
        "creationTimestamp": "2026-09-30T00:00:00Z",
        "managedFields": [
            {"manager": "kubectl", "operation": "Update", "time": "2026-10-10T00:00:40Z", "fieldsV1": {"f:data": {}}}
        ],
    }
    monkeypatch.setattr(_common, "_get_json", lambda args: {"metadata": meta})
    lines = tools._stale_env_refs(ctx, comp, workload)
    assert lines and lines[0].startswith(
        "Secret db-creds (read into environment variables) was written 40s after container api"
    )
    meta["managedFields"][0]["time"] = "2026-09-30T00:00:00Z"
    assert tools._stale_env_refs(ctx, comp, workload) == []
    # created just before the container started and rewritten 1s after it: the container holds the old value
    meta["creationTimestamp"] = "2026-10-09T23:59:59Z"
    meta["managedFields"][0]["time"] = "2026-10-10T00:00:01Z"
    lines = tools._stale_env_refs(ctx, comp, workload)
    assert lines and "was written 1s after container api" in lines[0]
    # rewritten in the same second the container started: reported as a possibility
    meta["managedFields"][0]["time"] = "2026-10-10T00:00:00Z"
    lines = tools._stale_env_refs(ctx, comp, workload)
    assert lines and "rewritten in the same second as container api" in lines[0] and "may still hold" in lines[0]
    # created (never rewritten) a few seconds after the container started, as at deploy time: not reported
    meta["creationTimestamp"] = meta["managedFields"][0]["time"] = "2026-10-10T00:00:03Z"
    assert tools._stale_env_refs(ctx, comp, workload) == []


def test_object_view_shows_configmap_data_secret_keys_and_spec(monkeypatch):
    ctx = make_ctx(simple_snapshot())
    objs = {
        "configmap": {
            "kind": "ConfigMap",
            "metadata": {"creationTimestamp": "2026-10-10T00:00:00Z"},
            "data": {"Corefile": "template ANY ANY x.ns.svc {\n rcode NXDOMAIN\n}"},
        },
        "secret": {"kind": "Secret", "metadata": {}, "data": {"password": "c2VjcmV0"}},
        "validatingwebhookconfiguration": {
            "kind": "ValidatingWebhookConfiguration",
            "metadata": {},
            "webhooks": [{"name": "w", "failurePolicy": "Fail"}],
        },
    }
    monkeypatch.setattr(_common, "_get_json", lambda args: objs[args[1]])
    cm = tools.run_object_view(ctx, "configmap/kube-system/coredns").lines
    assert cm[0].startswith("ConfigMap kube-system/coredns: created") and "rcode NXDOMAIN" in cm[1]
    sec = tools.run_object_view(ctx, "secret/ns/db").lines
    assert sec[1] == "keys ['password'] (values not read)" and "c2VjcmV0" not in " ".join(sec)
    wh = tools.run_object_view(ctx, "validatingwebhookconfiguration/w").lines
    assert any(ln.startswith("webhooks:") and "failurePolicy" in ln for ln in wh)


def test_a_write_before_the_application_was_deployed_is_marked_as_history(monkeypatch):
    from datetime import timedelta
    from types import SimpleNamespace

    from clients.jev_agent import evidence
    from clients.jev_diag.timeutil import now

    def iso(seconds_ago: float) -> str:
        return (now() - timedelta(seconds=seconds_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")

    comps = {"deployment/web": {"kind": "Deployment", "name": "web", "namespace": "ns", "created": iso(120)}}
    ctx = AgentContext(app_info={}, namespaces=["ns"], snapshot=SimpleNamespace(components=comps), overview={})
    old = tools._written(ctx, iso(10_000), iso(1_200))  # rewritten 20 minutes ago, the app is 2 minutes old
    new = tools._written(ctx, iso(10_000), iso(60))  # rewritten after the app was deployed
    assert "predates the application's deployment" in old and "predates" not in new
    assert not evidence._is_change("ConfigMap kube-system/coredns: " + old) and evidence._is_change(new)


def test_describe_reports_why_the_controller_cannot_create_pods(monkeypatch):
    # an admission webhook, quota or policy rejects pods before any exists: the cause sits on the Deployment and
    # its ReplicaSet, not on pods
    cause = (
        'Internal error occurred: failed calling webhook "pod-policy.example": failed to call webhook: Post '
        '"https://pod-policy-webhook.policy-system.svc:443/validate?timeout=5s": tls: failed to verify certificate: '
        "x509: certificate signed by unknown authority"
    )
    workload = {
        "status": {
            "conditions": [
                {"type": "Available", "status": "False", "reason": "MinimumReplicasUnavailable", "message": "no"},
                {"type": "ReplicaFailure", "status": "True", "reason": "FailedCreate", "message": cause},
            ]
        }
    }
    rs = {"metadata": {"name": "api-5d9", "ownerReferences": [{"kind": "Deployment", "name": "api"}]}}
    other = {"metadata": {"name": "api-gateway-77", "ownerReferences": [{"kind": "Deployment", "name": "api-gateway"}]}}
    events = [
        {
            "type": "Warning",
            "reason": "FailedCreate",
            "count": 9,
            "message": "Error creating: " + cause,
            "involvedObject": {"kind": "ReplicaSet", "name": "api-5d9"},
            "lastTimestamp": "2026-10-10T10:00:00Z",
        },
        {
            "type": "Warning",
            "reason": "FailedCreate",
            "count": 2,
            "message": "exceeded quota: other",
            "involvedObject": {"kind": "ReplicaSet", "name": "api-gateway-77"},
            "lastTimestamp": "2026-10-10T10:00:00Z",
        },
    ]
    monkeypatch.setattr(_common, "_get_json", lambda args: workload)
    monkeypatch.setattr(_common, "_items", lambda res, ns, **_: {"replicasets": [rs, other], "events": events}[res])
    lines = tools._controller_failures({"namespace": "ns", "kind": "Deployment", "name": "api"})
    assert len(lines) == 1  # the event repeats the condition; the causeless condition and api-gateway's are left out
    assert lines[0].startswith("Deployment condition ReplicaFailure=True FailedCreate") and lines[0].endswith(
        "unknown authority"
    )


def test_long_messages_keep_their_end_and_encoded_blobs_are_elided():
    text = "start " + "x" * 600 + " the decisive cause"
    short = tools._ends(text)
    assert short.startswith("start") and short.endswith("the decisive cause") and len(short) <= tools.MESSAGE_CHARS + 2
    assert tools._elide_encoded({"caBundle": "QUJD" * 60, "url": "https://a"}) == {
        "caBundle": "<240 chars of base64>",
        "url": "https://a",
    }


def test_describe_shows_ready_pods_left_on_an_older_template_by_a_paused_rollout():
    def rs(name, revision, ready, template):
        return {
            "metadata": {"name": name, "annotations": {"deployment.kubernetes.io/revision": str(revision)}},
            "status": {"readyReplicas": ready},
            "spec": {"template": template},
        }

    old = {"spec": {"hostAliases": [{"ip": "10.0.0.9", "hostnames": ["api"]}], "containers": [{"name": "web"}]}}
    new = {"spec": {"containers": [{"name": "web"}]}}
    lines = tools._template_mix(
        {"spec": {"paused": True}}, [rs("web-old", 2, 1, old), rs("web-new", 3, 1, new), rs("web-gone", 1, 0, old)]
    )
    assert lines[0].startswith("ready pods run 2 pod templates (rollout paused): revision 3 (web-new)")
    assert "older pod template (revision 2) still serve" in lines[1] and "hostAliases" in lines[1]
    assert tools._template_mix({}, [rs("web-new", 3, 2, new)]) == []  # a finished rollout says nothing


def test_describe_states_the_access_modes_of_the_claims_the_pods_mount(monkeypatch):
    ctx = make_ctx(simple_snapshot())
    workload = {
        "replicas": 2,
        "containers": [{"name": "api", "image": "demo/api:1"}],
        "volumes": [
            {"name": "data", "persistentVolumeClaim": {"claimName": "api-data"}},
            {"name": "tmp", "emptyDir": {}},
        ],
    }
    monkeypatch.setattr(investigate, "workload_detail", lambda kind, name, ns: workload)
    monkeypatch.setattr(investigate, "pod_details", lambda names, ns, **kw: ([], []))
    pvc = {"spec": {"accessModes": ["ReadWriteOnce"], "storageClassName": "local"}, "status": {"phase": "Bound"}}
    fake_kubectl(monkeypatch, {"get pvc api-data -n demo": pvc, "get ": RuntimeError("offline")})
    text = "\n".join(tools.run(ctx, "describe", "deployment/api").lines)
    assert (
        "volume data: PersistentVolumeClaim api-data Bound, access modes ['ReadWriteOnce'], storageClass local; "
        "mounted by every pod of this workload (2 replicas)" in text
    )


def test_related_objects_lists_what_surrounds_a_workload_failing_ones_first(monkeypatch):
    ctx = make_ctx(simple_snapshot())
    workload = {
        "spec": {
            "template": {
                "metadata": {"labels": {"app": "api"}},
                "spec": {
                    "serviceAccountName": "api",
                    "volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": "api-data"}}],
                    "containers": [{"name": "api", "envFrom": [{"configMapRef": {"name": "api-cfg"}}]}],
                },
            }
        }
    }
    rs = {
        "metadata": {"name": "api-5d9", "ownerReferences": [{"kind": "Deployment", "name": "api"}]},
        "spec": {"replicas": 1},
        "status": {
            "replicas": 0,
            "conditions": [{"type": "ReplicaFailure", "status": "True", "reason": "FailedCreate", "message": "denied"}],
        },
    }
    lists = {
        "replicasets": [rs],
        "persistentvolumeclaims": [
            {"metadata": {"name": "api-data"}, "spec": {"accessModes": ["ReadWriteOnce"]}, "status": {"phase": "Bound"}}
        ],
        "rolebindings": [
            {
                "metadata": {"name": "api-read"},
                "roleRef": {"kind": "Role", "name": "reader"},
                "subjects": [{"kind": "ServiceAccount", "name": "api"}],
            }
        ],
        "services": [
            {"metadata": {"name": "api"}, "spec": {"selector": {"app": "api"}}},
            {"metadata": {"name": "web"}, "spec": {"selector": {"app": "web"}}},
        ],
        "networkpolicies": [{"metadata": {"name": "deny"}, "spec": {"podSelector": {"matchLabels": {"app": "api"}}}}],
        "poddisruptionbudgets": [],
        "horizontalpodautoscalers": [],
        "events": [
            {
                "type": "Warning",
                "reason": "FailedCreate",
                "message": "Error creating: denied",
                "involvedObject": {"kind": "ReplicaSet", "name": "api-5d9"},
            }
        ],
    }
    monkeypatch.setattr(_common, "_get_json", lambda args: workload)
    monkeypatch.setattr(_common, "_items", lambda res, ns, **_: lists[res])
    lines = tools.run_related_objects(ctx, "deployment/api").lines
    assert lines[0].startswith("ReplicaSet api-5d9 (owned): 0/1 ready; ReplicaFailure=True FailedCreate: denied")
    assert "Warning FailedCreate x1" in lines[0]
    text = "\n".join(lines)
    assert "PersistentVolumeClaim api-data (volume data): Bound, modes ['ReadWriteOnce']" in text
    assert "ConfigMap api-cfg (environment)" in text and "RoleBinding api-read (grants Role reader)" in text
    assert "Service api (selects its pods)" in text and "Service web" not in text
    assert "NetworkPolicy deny (selects its pods)" in text
