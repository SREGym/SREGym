"""LLM-crafted read-only tools: validation, execution, exploration, and the CRAFT menu option."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from clients.jev_agent import crafted, discovery, tools
from clients.jev_agent.tools import _podexec
from clients.jev_agent.types import AgentContext, ToolResult
from clients.jev_diag import collector
from tests.clients.test_jev_agent_loop import (
    Q_NEXT,
    FakeLLM,
    cfg,
    fake_tools,
    investigator,
)

RESOURCES = {"pods", "deployments", "persistentvolumeclaims", "natsstreams", "configmaps"}


def ctx_with(comps: dict) -> AgentContext:
    return AgentContext(
        app_info={"app_name": "demo"}, namespaces=["ns"], snapshot=SimpleNamespace(components=comps), overview={}
    )


COMPS = {
    "deployment/cache": {
        "kind": "Deployment",
        "name": "cache",
        "namespace": "ns",
        "containers": [{"name": "cache", "image": "valkey/valkey:8", "limits": {"memory": "64Mi"}}],
        "pods": [{"name": "cache-5d8f7c9b4-abcde", "phase": "Running", "ready": "1/1"}],
        "resource_usage": [{"pod": "cache-5d8f7c9b4-abcde", "container": "cache", "memory": "10Mi"}],
        "services": [{"name": "cache", "ports": ["6379->6379/TCP"]}],
    },
    "deployment/api": {
        "kind": "Deployment",
        "name": "api",
        "namespace": "ns",
        "containers": [{"name": "api", "image": "shop/api:1"}],
        "pods": [{"name": "api-6c5bcfcb8f-qbrwg", "phase": "Running", "ready": "1/1"}],
        "services": [{"name": "api", "ports": ["8080->8080/TCP"]}],
    },
}


def good_spec(**over):
    spec = {
        "name": "cache_info",
        "reveals": "Whether the cache answers and how much memory it uses.",
        "applies_to": {"image_contains": "valkey"},
        "steps": [{"op": "exec", "command": ["valkey-cli", "-p", "6379", "info", "memory"]}],
    }
    spec.update(over)
    return spec


# --------------------------------------------------------------------------- validation


def test_validate_accepts_read_only_specs():
    tool, why = crafted.validate(good_spec(), RESOURCES)
    assert tool is not None and why == "" and tool.tool_name == "x_cache_info"
    k8s = good_spec(
        name="claims", steps=[{"op": "k8s_get", "resource": "persistentvolumeclaims", "fields": ["spec.accessModes"]}]
    )
    assert crafted.validate(k8s, RESOURCES)[0] is not None


@pytest.mark.parametrize(
    "step, reason",
    [
        ({"op": "exec", "command": ["valkey-cli", "config", "set", "maxmemory", "1"]}, "forbidden"),
        ({"op": "exec", "command": ["valkey-cli", "flushall"]}, "forbidden"),
        ({"op": "exec", "command": ["sh", "-c", "ls"]}, "read-only list"),
        ({"op": "exec", "command": ["cat", "/var/run/secrets/kubernetes.io/serviceaccount/token"]}, "credential"),
        ({"op": "exec", "command": ["valkey-cli", "ping;", "x"]}, "metacharacters"),
        ({"op": "exec", "command": ["curl", "-X", "POST", "http://x/"]}, "forbidden"),
        ({"op": "exec", "command": ["redis-cli", "-a", "pw", "ping"]}, "forbidden"),
        ({"op": "k8s_get", "resource": "secrets"}, "not allowed"),
        ({"op": "k8s_get", "resource": "widgets"}, "not served"),
        ({"op": "drop_table"}, "unknown op"),
        ({"op": "http_get", "port": "80", "path": "/health"}, "integer port"),
    ],
)
def test_validate_rejects_anything_that_could_change_state_or_leak(step, reason):
    tool, why = crafted.validate(good_spec(steps=[step]), RESOURCES)
    assert tool is None and reason in why


def test_validate_rejects_bad_shape_and_taken_names():
    assert crafted.validate(good_spec(name="Bad Name"), RESOURCES)[0] is None
    assert crafted.validate(good_spec(steps=[]), RESOURCES)[0] is None
    assert crafted.validate(good_spec(), RESOURCES, taken={"x_cache_info"})[0] is None


# --------------------------------------------------------------------------- running


def test_run_exec_k8s_and_logs_steps(monkeypatch):
    ctx = ctx_with(COMPS)
    calls = []

    def fake_exec(ns, pod, container, cmd):
        calls.append(cmd)
        return 0, "# Memory\nused_memory_human:1.2M\nmaxmemory_human:0B"

    def fake_kubectl(args, timeout=45):
        if args[:2] == ["get", "persistentvolumeclaims"]:
            return json.dumps({"items": [{"metadata": {"name": "data"}, "spec": {"accessModes": ["ReadWriteOnce"]}}]})
        if args[0] == "logs":
            return "ok\nNOAUTH Authentication required\nok\nNOAUTH Authentication required"
        raise AssertionError(args)

    monkeypatch.setattr(_podexec, "_exec", fake_exec)
    monkeypatch.setattr(crafted.collector, "run_kubectl", fake_kubectl)
    monkeypatch.setattr(collector, "run_kubectl", fake_kubectl)
    tool, _ = crafted.validate(
        good_spec(
            steps=[
                {"op": "exec", "command": ["valkey-cli", "-p", "6379", "info", "memory"]},
                {"op": "k8s_get", "resource": "persistentvolumeclaims", "fields": ["spec.accessModes"]},
                {"op": "logs_grep", "pattern": "NOAUTH"},
            ]
        ),
        RESOURCES,
    )
    res = crafted.run(ctx, tool, "deployment/cache")
    text = "\n".join(res.lines)
    assert calls == [["valkey-cli", "-p", "6379", "info", "memory"]]
    assert "used_memory_human:1.2M" in text
    assert 'persistentvolumeclaims/data: spec.accessModes=["ReadWriteOnce"]' in text
    assert "2 of 4 log lines match /NOAUTH/" in text
    assert crafted.useful(res)


def test_run_skips_exec_without_headroom_and_reports_failed_steps(monkeypatch):
    comps = json.loads(json.dumps(COMPS))
    comps["deployment/cache"]["resource_usage"][0]["memory"] = "63Mi"
    ctx = ctx_with(comps)
    monkeypatch.setattr(_podexec, "_exec", lambda *a: pytest.fail("must not exec without headroom"))
    tool, _ = crafted.validate(good_spec(), RESOURCES)
    res = crafted.run(ctx, tool, "deployment/cache")
    assert "skipped" in res.lines[0] and not crafted.useful(res)


def test_applies_to_role_and_image():
    ctx = ctx_with(COMPS)
    tool, _ = crafted.validate(good_spec(), RESOURCES)
    assert crafted.applies(ctx, tool, "deployment/cache") and not crafted.applies(ctx, tool, "deployment/api")


# --------------------------------------------------------------------------- explore and craft


def test_explore_keeps_tools_that_pass_validation_and_smoke_test(monkeypatch):
    ctx = ctx_with(COMPS)
    monkeypatch.setattr(discovery, "api_resources", lambda: RESOURCES)
    monkeypatch.setattr(discovery.collector, "run_kubectl", lambda args, timeout=45: json.dumps({"items": []}))
    ctx.extra["prom_call"] = lambda q: "{'resultType': 'vector', 'result': []}"
    monkeypatch.setattr(_podexec, "_exec", lambda ns, pod, c, cmd: (0, "PONG"))
    answer = {
        "tools": [
            good_spec(name="cache_ping", steps=[{"op": "exec", "command": ["valkey-cli", "ping"]}]),
            good_spec(name="cache_flush", steps=[{"op": "exec", "command": ["valkey-cli", "flushall"]}]),
            good_spec(name="no_target", applies_to={"role": "kafka"}),
        ]
    }
    registry: dict = {}
    res = discovery.explore(FakeLLM([json.dumps(answer)]), ctx, registry, {"deployment/cache": 1.0})
    assert [k["tool"] for k in res.kept] == ["x_cache_ping"] and "x_cache_ping" in registry
    assert {d["reason"].split()[0] for d in res.dropped} == {"forbidden", "no"}
    assert res.inventory["components"][0]["id"] in COMPS


def test_craft_option_runs_an_llm_tool_and_records_the_observation(monkeypatch):
    llm = FakeLLM(
        [
            json.dumps(
                {
                    "tool": {
                        "name": "db_ping",
                        "reveals": "Does db answer.",
                        "steps": [{"op": "logs_grep", "pattern": "refused"}],
                    },
                    "component": "deployment/db",
                    "reason": "errors name db",
                }
            )
        ]
    )
    ft = fake_tools()
    monkeypatch.setattr(
        crafted, "run", lambda ctx, tool, cid: ToolResult(lines=[f"3 of 10 log lines match /refused/ on {cid}"])
    )
    monkeypatch.setattr(crafted, "applies", lambda ctx, tool, cid: True)

    def script(i, s, q):
        keys = list(q[Q_NEXT].criteria) if Q_NEXT in q else []
        if "craft_tool" in keys:
            return dict(nxt=lambda k: "craft_tool", enough=0.1)
        return dict(enough=0.1)

    inv, _ = investigator(script, tools=ft, llm=llm, config=cfg(max_steps=6, max_llm_calls=5, craft=True, max_craft=1))
    from clients.jev_agent import policy

    monkeypatch.setattr(policy, "CRAFT_MIN_STEPS", 1)
    res = inv.run()
    assert len(res.crafts) == 1 and res.crafts[0]["tool"] == "x_db_ping"
    obs = [o for o in res.observations if o.tool == "x_db_ping"]
    assert obs and obs[0].target == "deployment/db" and "match /refused/" in obs[0].lines[0]
    assert "x_db_ping" in ft.REGISTRY


def test_in_task_tool_applies_only_to_its_target():
    ctx = ctx_with(COMPS)
    tool, _ = crafted.validate(good_spec(applies_to={}), RESOURCES)
    assert crafted.applies(ctx, tool, "deployment/api")
    tool.only_target = "deployment/cache"
    assert crafted.applies(ctx, tool, "deployment/cache") and not crafted.applies(ctx, tool, "deployment/api")


def test_missing_binary_is_not_a_useful_smoke_test():
    assert not crafted.useful(
        ToolResult(lines=["`kafka-topics.sh --describe` in kafka-1 (rc=127): env: No such file or directory"])
    )
    assert crafted.useful(
        ToolResult(lines=["`valkey-cli INFO memory` in cache-1 (rc=0): NOAUTH Authentication required."])
    )


def test_explore_keeps_smoke_results_as_observations(monkeypatch):
    ctx = ctx_with(COMPS)
    monkeypatch.setattr(discovery, "api_resources", lambda: RESOURCES)
    monkeypatch.setattr(discovery.collector, "run_kubectl", lambda args, timeout=45: json.dumps({"items": []}))
    ctx.extra["prom_call"] = lambda q: "{'resultType': 'vector', 'result': []}"
    monkeypatch.setattr(_podexec, "_exec", lambda ns, pod, c, cmd: (0, "NOAUTH Authentication required."))
    answer = {"tools": [good_spec(name="cache_info", steps=[{"op": "exec", "command": ["valkey-cli", "info"]}])]}
    res = discovery.explore(FakeLLM([json.dumps(answer)]), ctx, {}, {})
    assert [(n, t) for n, t, _ in res.trials] == [("x_cache_info", "deployment/cache")]
    assert "NOAUTH" in res.trials[0][2].lines[0]


# --------------------------------------------------------------------------- HTTP clients, http_get fallback


@pytest.mark.parametrize(
    "command",
    [
        ["curl", "--silent", "--show-error", "--max-time", "3", "http://127.0.0.1:14269/"],
        ["curl", "-fsS", "http://127.0.0.1:16686/api/services"],
        ["curl", "-sS", "-H", "Accept: application/json", "http://opensearch:9200/_cat/indices?format=json&h=health"],
        ["wget", "-q", "-O", "-", "-T", "5", "http://127.0.0.1:14269/"],
        ["ps", "-o", "pid,etime,rss,args"],
    ],
)
def test_read_only_http_and_process_commands_are_accepted(command):
    tool, why = crafted.validate(good_spec(steps=[{"op": "exec", "command": command}]), RESOURCES)
    assert tool is not None, why


@pytest.mark.parametrize(
    "command",
    [
        ["curl", "-o", "/tmp/x", "http://a/"],
        ["curl", "-T", "/etc/hosts", "http://a/"],
        ["curl", "-d", "x=1", "http://a/"],
        ["curl", "--data=x", "http://a/"],
        ["curl", "-u", "admin:pw", "http://a/"],
        ["curl", "-H", "Authorization: Bearer x", "http://a/"],
        ["curl", "--silent", "gopher://127.0.0.1:11211/_stats"],
        ["curl", "http://a/", "http://b/"],
        ["curl", "http://a/admin/shutdown"],
        ["wget", "-O", "/tmp/x", "http://a/"],
        ["wget", "--post-data=x", "http://a/"],
    ],
)
def test_http_clients_reject_anything_but_a_get_to_stdout(command):
    tool, why = crafted.validate(good_spec(steps=[{"op": "exec", "command": command}]), RESOURCES)
    assert tool is None and why


def test_http_get_accepts_query_strings_and_rejects_mutating_paths():
    ok = good_spec(steps=[{"op": "http_get", "port": 9200, "path": "/_cat/indices?format=json&h=health,index"}])
    assert crafted.validate(ok, RESOURCES)[0] is not None
    bad = good_spec(steps=[{"op": "http_get", "port": 8500, "path": "/v1/agent/leave'; x"}])
    assert crafted.validate(bad, RESOURCES)[0] is None
    bad = good_spec(steps=[{"op": "http_get", "port": 8500, "path": "/v1/kv/delete"}])
    assert crafted.validate(bad, RESOURCES)[0] is None


def test_http_get_falls_back_to_a_client_inside_the_namespace_when_the_proxy_is_refused(monkeypatch):
    comps = json.loads(json.dumps(COMPS))
    comps["deployment/cache"]["services"] = [{"name": "cache", "ports": ["80->6380/TCP"]}]
    ctx = ctx_with(comps)
    proxied = []

    def fake_kubectl(args, timeout=45):
        proxied.append(args)
        raise RuntimeError("Error from server (Forbidden): proxying through a workload is not allowed")

    calls = []

    def fake_exec(ns, pod, container, cmd):
        calls.append((pod, cmd))
        if cmd[0] == "curl":
            return 127, "exec: curl: executable file not found in $PATH"
        if pod.startswith("cache"):
            return 1, ""  # wget ran in the component's own pod but could not connect on localhost
        return 0, '{"status":"ok"}'

    monkeypatch.setattr(crafted.collector, "run_kubectl", fake_kubectl)
    monkeypatch.setattr(_podexec, "_exec", fake_exec)
    tool, _ = crafted.validate(
        good_spec(applies_to={}, steps=[{"op": "http_get", "port": 80, "path": "/health"}]), RESOURCES
    )
    res = crafted.run(ctx, tool, "deployment/cache")
    assert calls[0] == (
        "cache-5d8f7c9b4-abcde",
        ["curl", "-sS", "-m", "5", "-w", "\n[http %{http_code}]", "http://127.0.0.1:6380/health"],
    )
    assert calls[1][1][0] == "wget" and calls[1][1][-1] == "http://127.0.0.1:6380/health"
    assert calls[-1][0].startswith("api") and calls[-1][1][-1] == "http://cache.ns.svc:80/health"
    assert "with wget in api-" in res.lines[0] and '{"status":"ok"}' in res.lines[1] and crafted.useful(res)
    # the refusal is remembered: the next http_get goes straight to the pods
    crafted.run(ctx, tool, "deployment/cache")
    assert len(proxied) == 1


# --------------------------------------------------------------------------- craft retry, explore cache


def test_craft_rewrites_a_rejected_tool_once_with_the_reason(monkeypatch):
    ctx = ctx_with(COMPS)
    ctx.extra["api_resources"] = RESOURCES
    monkeypatch.setattr(_podexec, "_exec", lambda ns, pod, c, cmd: (0, "PONG"))
    bad = {
        "tool": good_spec(name="cache_ping", steps=[{"op": "exec", "command": ["curl", "-X", "POST", "http://a/"]}]),
        "component": "deployment/cache",
    }
    good = {
        "tool": good_spec(name="cache_ping", steps=[{"op": "exec", "command": ["valkey-cli", "ping"]}]),
        "component": "deployment/cache",
    }
    llm = FakeLLM([json.dumps(bad), json.dumps(good)])
    res = discovery.craft_now(llm, ctx, {}, {"observations": []})
    assert res.tool == "x_cache_ping" and res.error is None and "PONG" in res.result.lines[0]
    second = json.loads(llm.prompts[1][1])
    assert "forbidden" in second["previous_answer_rejected_because"] and "deployment/cache" in second["components"]


def test_explore_cache_reuses_kept_tools_without_an_llm_call(monkeypatch, tmp_path):
    monkeypatch.setattr(discovery, "api_resources", lambda: RESOURCES)
    monkeypatch.setattr(discovery.collector, "run_kubectl", lambda args, timeout=45: json.dumps({"items": []}))
    monkeypatch.setattr(_podexec, "_exec", lambda ns, pod, c, cmd: (0, "PONG"))
    answer = {"tools": [good_spec(name="cache_ping", steps=[{"op": "exec", "command": ["valkey-cli", "ping"]}])]}

    def fresh():
        ctx = ctx_with(COMPS)
        ctx.extra["prom_call"] = lambda q: "{'resultType': 'vector', 'result': []}"
        return ctx

    first = discovery.explore(FakeLLM([json.dumps(answer)]), fresh(), {}, {}, cache_dir=str(tmp_path))
    assert first.cache == "miss+saved" and [k["tool"] for k in first.kept] == ["x_cache_ping"]
    llm = FakeLLM([])
    registry: dict = {}
    second = discovery.explore(llm, fresh(), registry, {}, cache_dir=str(tmp_path))
    assert second.cache == "hit" and llm.calls == 0 and "x_cache_ping" in registry and second.trials
    # a different environment (another image) does not reuse it
    comps = json.loads(json.dumps(COMPS))
    comps["deployment/cache"]["containers"][0]["image"] = "redis:7"
    ctx = ctx_with(comps)
    ctx.extra["prom_call"] = lambda q: "{'resultType': 'vector', 'result': []}"
    other = discovery.explore(FakeLLM([json.dumps({"tools": []})]), ctx, {}, {}, cache_dir=str(tmp_path))
    assert other.cache is None


def test_stale_cache_falls_back_to_exploring(monkeypatch, tmp_path):
    monkeypatch.setattr(discovery, "api_resources", lambda: RESOURCES)
    monkeypatch.setattr(discovery.collector, "run_kubectl", lambda args, timeout=45: json.dumps({"items": []}))
    ctx = ctx_with(COMPS)
    ctx.extra["prom_call"] = lambda q: "{'resultType': 'vector', 'result': []}"
    key = discovery.cache_key(discovery.inventory(ctx, {}))
    discovery.save_cached(
        str(tmp_path), key, [good_spec(name="gone", steps=[{"op": "exec", "command": ["valkey-cli", "ping"]}])], {}
    )
    monkeypatch.setattr(_podexec, "_exec", lambda ns, pod, c, cmd: (127, "valkey-cli: not found"))
    llm = FakeLLM([json.dumps({"tools": []})])
    res = discovery.explore(llm, ctx, {}, {}, cache_dir=str(tmp_path))
    assert res.cache == "stale" and llm.calls == 1


def test_propose_then_adopt_equals_explore_and_needs_no_fresh_state(monkeypatch, tmp_path):
    monkeypatch.setattr(discovery, "api_resources", lambda: RESOURCES)
    monkeypatch.setattr(discovery.collector, "run_kubectl", lambda args, timeout=45: json.dumps({"items": []}))
    monkeypatch.setattr(_podexec, "_exec", lambda ns, pod, c, cmd: (0, "PONG"))
    answer = {"tools": [good_spec(name="cache_ping", steps=[{"op": "exec", "command": ["valkey-cli", "ping"]}])]}
    early = ctx_with(COMPS)  # the snapshot at the start of the delay
    early.extra["prom_call"] = lambda q: "{'resultType': 'vector', 'result': []}"
    prop = discovery.propose(FakeLLM([json.dumps(answer)]), early, {}, str(tmp_path))
    assert prop.cache is None and len(prop.specs) == 1 and "pods" in prop.resources
    later = ctx_with(COMPS)  # the context after the delay: validation and smoke tests use it
    registry: dict = {}
    res = discovery.adopt(FakeLLM([]), later, registry, {}, prop, str(tmp_path))
    assert [k["tool"] for k in res.kept] == ["x_cache_ping"] and res.cache == "miss+saved"
    assert later.extra["api_resources"] == prop.resources and res.trials


def test_early_explore_runs_during_the_delay_without_recording(monkeypatch, tmp_path):
    from clients.jev_agent import driver
    from clients.jev_diag import collector

    written = []
    monkeypatch.setattr(collector, "_RAW_RECORDER", SimpleNamespace(write=written.append))
    monkeypatch.setattr(
        collector.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0, stdout="{}", stderr="")
    )

    def fake_snapshot(app_info, **kw):
        collector.run_kubectl(["get", "pods"])
        return SimpleNamespace(components=COMPS)

    monkeypatch.setattr(driver, "collect_snapshot", fake_snapshot)
    monkeypatch.setattr(
        discovery,
        "propose",
        lambda llm, ctx, reg, cache: discovery.Proposal(inventory={}, resources=set(), key="k", specs=[{"name": "t"}]),
    )
    early = driver._EarlyExplore({"app_name": "demo"}, ["ns"], object(), SimpleNamespace(cache_dir=""), tools)
    prop = early.result(5)
    assert prop is not None and prop.specs == [{"name": "t"}] and early.info["specs"] == 1
    assert written == []  # the early read is not in the raw recording
    collector.run_kubectl(["get", "pods"])
    assert len(written) == 1  # recording resumes afterwards


def test_missing_binary_with_kubectl_trailer_is_not_useful(monkeypatch):
    ctx = ctx_with(COMPS)
    monkeypatch.setattr(
        _podexec,
        "_exec",
        lambda ns, pod, c, cmd: (
            127,
            "env: ‘kafka-topics.sh’: No such file or directory\ncommand terminated with exit code 127",
        ),
    )
    tool, _ = crafted.validate(
        good_spec(applies_to={}, steps=[{"op": "exec", "command": ["kafka-topics.sh", "--describe"]}]), RESOURCES
    )
    res = crafted.run(ctx, tool, "deployment/cache")
    assert len(res.lines) == 1 and not crafted.useful(res)
