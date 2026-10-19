"""jev_diag v2: shared plumbing with v1, and the driver hand-over."""

from __future__ import annotations

import argparse
from pathlib import Path

import msgspec

from clients.jev_diag import collector as v1_collector
from clients.jev_diag import driver as v1_driver
from clients.jev_diag.classifier import build_component_questions as v1_questions
from clients.jev_diag.v2 import collector as v2_collector
from clients.jev_diag.v2 import driver as v2_driver
from clients.jev_diag.v2.classifier import build_component_questions as v2_questions

NS = "shop"


def deployment(name: str, ready: int = 1) -> dict:
    return {
        "kind": "Deployment",
        "metadata": {"name": name, "namespace": NS, "uid": f"uid-{name}"},
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": {"app": name}},
            "template": {"metadata": {"labels": {"app": name}}, "spec": {"containers": [{"name": name, "image": "i"}]}},
        },
        "status": {"replicas": 1, "readyReplicas": ready},
    }


def snapshot(module) -> object:
    comps = {}
    for obj in (deployment("frontend", ready=0), deployment("cart")):
        c = module.summarize_workload(obj)
        comps[module.component_id(c["kind"], c["name"])] = c
    comps["deployment/frontend"]["signals"] = ["only 0 of 1 desired replicas are ready"]
    for c in comps.values():
        c["healthy"] = not c.get("signals")
    return module.ClusterSnapshot(
        app={"name": "shop", "namespaces": [NS], "description": "d"},
        components=comps,
        cluster={"nodes": [], "firing_alerts": []},
    )


def test_v2_reads_the_cluster_through_v1s_single_kubectl_runner():
    # recording, replay and tracing are set up once by the driver and must cover v2's reads too
    assert v2_collector.run_kubectl is v1_collector.run_kubectl
    assert v2_collector.set_kubectl_replay is v1_collector.set_kubectl_replay


def test_v2_triage_question_matches_v1_on_the_same_snapshot():
    a = {k: msgspec.to_builtins(q) for k, q in v1_questions(snapshot(v1_collector)).items()}
    b = {k: msgspec.to_builtins(q) for k, q in v2_questions(snapshot(v2_collector)).items()}
    assert a == b


def test_driver_hands_mode_v2_to_the_v2_run(monkeypatch):
    seen = {}

    def fake_run(args, trace, prefix, results, started, host):
        seen["host"] = host
        return "ok"

    monkeypatch.setattr(v2_driver, "run", fake_run)
    args = argparse.Namespace(mode="v2")
    assert v1_driver.run(args, None, Path("/tmp/x"), {}, 0.0) == "ok"
    assert seen["host"].submit_diagnosis is v1_driver.submit_diagnosis


def _comp(name: str, created: str, **extra) -> dict:
    return {"kind": "Deployment", "name": name, "namespace": NS, "created": created, "signals": [], **extra}


def test_revision_diff_without_a_spec_write_is_a_change_and_a_restart_is_not():
    from clients.jev_diag.timeutil import set_now
    from clients.jev_diag.v2 import changes

    set_now(changes.parse_time("2026-10-19T08:00:00Z"))
    try:
        comps = {
            "deployment/frontend": _comp("frontend", "2026-10-19T07:50:00Z"),
            "daemonset/agent": {**_comp("agent", "2026-10-19T07:50:00Z"), "kind": "DaemonSet"},
        }
        removed = changes.object_change(
            "deployment/frontend",
            "Deployment",
            "frontend",
            NS,
            {"creationTimestamp": "2026-10-19T07:50:00Z"},
            affects=["deployment/frontend"],
            extra={"template_changes": ["container frontend env CART_ADDR removed"]},
            revised_at="2026-10-19T07:58:00Z",
        )
        restart = changes.object_change(
            "daemonset/agent",
            "DaemonSet",
            "agent",
            NS,
            {"creationTimestamp": "2026-10-19T07:50:00Z"},
            affects=["daemonset/agent"],
            extra={
                "template_changes": ["template annotation kubectl.kubernetes.io/restartedAt added: x"],
                "restart_only": True,
            },
            revised_at="2026-10-19T07:59:00Z",
        )
        cluster = {"_changes": [removed, restart]}
        changes.rank_changes(comps, cluster)
    finally:
        set_now(None)
    assert any(
        s.startswith("pod template changed") and "CART_ADDR removed" in s
        for s in comps["deployment/frontend"]["signals"]
    )
    assert comps["daemonset/agent"]["signals"][0].startswith("rolling restart")
    assert comps["deployment/frontend"].get("most_recent_change") and not comps["daemonset/agent"].get(
        "most_recent_change"
    )


def test_a_change_made_before_the_application_existed_is_dropped():
    from clients.jev_diag.timeutil import set_now
    from clients.jev_diag.v2 import changes

    set_now(changes.parse_time("2026-10-19T08:00:00Z"))
    try:
        comps = {"deployment/frontend": _comp("frontend", "2026-10-19T07:50:00Z")}
        leftover = {
            "object": "configmap/kube-system/coredns",
            "kind": "ConfigMap",
            "name": "coredns",
            "namespace": "kube-system",
            "at": "2026-10-19T07:30:00Z",
            "affects": ["deployment/frontend"],
            "modified_after_creation_s": 100,
        }
        cluster = {"_changes": [leftover]}
        ranked = changes.rank_changes(comps, cluster)
    finally:
        set_now(None)
    assert ranked == [] and cluster["recent_changes"] == [] and comps["deployment/frontend"]["signals"] == []


def test_probe_description_carries_timing_with_defaults_for_unset_fields():
    from clients.jev_diag.v2.checks import describe_probe

    text = describe_probe({"httpGet": {"path": "/health", "port": 8080}, "periodSeconds": 1, "failureThreshold": 1})
    assert text.startswith("httpGet HTTP :8080/health")
    assert "periodSeconds=1" in text and "failureThreshold=1" in text
    assert "initialDelaySeconds=0 (default)" in text and "timeoutSeconds=1 (default)" in text


def test_evidence_cap_keeps_some_logs_events_and_dependencies():
    from clients.jev_diag.v2.tree import MAX_EVIDENCE, cap_evidence

    head = [f"setting {i}" for i in range(40)]
    logs = [f"log {i}" for i in range(10)]
    events = [f"event {i}" for i in range(5)]
    deps = ["dependency x looks healthy on its own"]
    out = cap_evidence(head, logs, events, deps, ["env A=1"])
    assert len(out) == MAX_EVIDENCE and out[0] == "setting 0"
    assert sum(i.startswith("log ") for i in out) == 4 and sum(i.startswith("event ") for i in out) == 3
    assert deps[0] in out
    short = cap_evidence(["s"], ["l"], [], [], ["env A=1"])
    assert short == ["s", "l", "env A=1"]


def test_a_line_naming_several_components_points_at_the_one_it_called():
    from clients.jev_diag.v2.derive import _called

    aliases = {"shipping": "s", "quote": "q", "email": "e", "cart": "c", "valkey-cart": "v"}
    line = "shipping quote failure: failed POST to email service"
    assert _called(line, aliases, {"s", "q", "e"}) == {"e"}
    assert _called("failed to connect to valkey-cart:6379 for cart", aliases, {"c", "v"}) == {"v"}
    assert _called("cart and email unavailable", aliases, {"c", "e"}) == set()  # no called position: keep all


def _diag(component: str, fact: str | None = None, **kw):
    from clients.jev_diag.v2.classifier import ChoiceResult, Diagnosis

    return Diagnosis(
        component=component,
        component_result=ChoiceResult(choice=component, confidence=0.9, probabilities={component: 0.9}),
        fault_visible=None,
        category_result=ChoiceResult(choice="config_value", confidence=0.9, probabilities={"config_value": 0.9}),
        evidence_result=ChoiceResult(choice="E1", confidence=0.9, probabilities={"E1": 0.9}) if fact else None,
        evidence=[{"id": "E1", "text": fact}] if fact else [],
        model="m",
        input_tokens=None,
        output_tokens=None,
        investigation={"steps": []},
        **kw,
    )


def _snap(comps: dict):
    return v2_collector.ClusterSnapshot(app={"name": "a", "namespaces": [NS]}, components=comps, cluster={})


def test_report_leads_with_the_changed_workload_and_has_no_fault_type_line():
    from clients.jev_diag.v2.report import build_report

    comp = {
        **_comp("frontend", "t"),
        "evidence_kinds": ["change"],
        "signals": ["only 0 of 1 desired replicas are ready"],
        "template_changes": {"changes": ["container frontend env CART_ADDR removed (was cart:8080)"]},
    }
    text = build_report(_snap({"deployment/frontend": comp}), _diag("deployment/frontend", "env CART_ADDR removed"))
    assert text.startswith("Root cause: Deployment `frontend` in namespace `shop`: env CART_ADDR removed.")
    assert "Fault type" not in text and "Effect: only 0 of 1 desired replicas are ready." in text


def test_report_names_the_owning_component_through_its_configmap_and_an_outside_object_first():
    from clients.jev_diag.v2.report import build_report

    db = {**_comp("db", "t"), "services": [{"name": "db"}], "signals": []}
    fo = {
        "kind": "ConfigMap",
        "name": "db",
        "namespace": NS,
        "signal": "ConfigMap db was modified",
        "components": ["deployment/db"],
    }
    text = build_report(
        _snap({"deployment/db": db}), _diag("deployment/db", "mongod.conf tls requireTLS", fault_object=fo)
    )
    assert text.startswith(
        "Root cause: Service `db` (Deployment `db`) in namespace `shop`, through its ConfigMap `db`:"
    )
    caller = {**_comp("user", "t"), "referenced_by_errors_from": {"deployment/api": {"dns resolution failure": 27}}}
    api = _comp("api", "t")
    dns = {"kind": "ConfigMap", "name": "coredns", "namespace": "kube-system", "signal": "rule answers NXDOMAIN"}
    text = build_report(
        _snap({"deployment/user": caller, "deployment/api": api}), _diag("deployment/user", fault_object=dns)
    )
    assert text.startswith("Root cause: ConfigMap `coredns` in namespace `kube-system`, acting on Deployment `user`")
    assert "Affected (symptoms only, not causes): Deployment `api` in namespace `shop`: dns resolution failure" in text


def test_without_the_decision_model_v2_still_reports_from_code():
    from clients.jev_diag.v2.tree import IterativeDiagnoser

    class Down:
        model = "m"

        def system_one(self, *a, **k):
            raise RuntimeError("Decisions API HTTP 402: insufficient credits")

    changed = {
        **_comp("frontend", "t"),
        "evidence_kinds": ["change", "symptoms"],
        "healthy": False,
        "nearest_change_before_symptoms": True,
        "signals": ["pod template changed 75s ago; pod template changes: container frontend env CART_ADDR removed"],
    }
    snap = _snap({"deployment/frontend": changed, "deployment/cart": {**_comp("cart", "t"), "healthy": True}})
    d = IterativeDiagnoser(Down(), fetch_detail=lambda s, c: {}).diagnose(snap)
    assert d.component == "deployment/frontend" and "decision model unavailable" in d.investigation["fallback"]
    assert d.text.startswith("Root cause: Deployment `frontend`") and "CART_ADDR removed" in d.text


class _Trace:
    from contextlib import contextmanager

    @contextmanager
    def timed(self, *a, **k):
        yield {}


def _run_wait(monkeypatch, samples):
    from clients.jev_diag.v2 import trend

    clock = {"t": 0.0}
    monkeypatch.setattr(v2_driver.time, "sleep", lambda s: clock.__setitem__("t", clock["t"] + s))
    monkeypatch.setattr(v2_driver.time, "monotonic", lambda: clock["t"])
    seq = iter(samples)
    monkeypatch.setattr(trend, "collect_sample", lambda ns: next(seq))
    args = argparse.Namespace(start_delay=120.0)
    v2_driver.wait(args, {"namespaces": [NS]}, _Trace(), False, None)
    return clock["t"]


def test_adaptive_wait_reads_early_once_the_same_pods_stay_abnormal(monkeypatch):
    crash = {"pods": {"shop/a-1": {"phase": "Running", "restarts": {"a": 3}}}}
    ok = {"pods": {"shop/a-1": {"phase": "Running", "restarts": {"a": 0}}}}
    assert _run_wait(monkeypatch, [crash, crash]) == 60  # 45 s minimum, one poll
    assert _run_wait(monkeypatch, [ok] * 10) == 120  # nothing abnormal in pods: v1's full wait


def test_outside_objects_link_to_the_components_they_act_on(monkeypatch):
    from clients.jev_diag.v2 import outside

    ingress = {
        "metadata": {"name": "web", "namespace": NS, "creationTimestamp": "2026-10-19T07:50:00Z"},
        "spec": {
            "rules": [
                {
                    "host": "shop",
                    "http": {
                        "paths": [{"path": "/", "backend": {"service": {"name": "front", "port": {"number": 80}}}}]
                    },
                }
            ]
        },
    }
    default_pc = {"metadata": {"name": "urgent"}, "value": 1000000, "globalDefault": True}
    items = {"ingresses": [ingress], "priorityclasses": [default_pc], "daemonsets": [], "cronjobs": []}
    monkeypatch.setattr(outside, "kubectl_items", lambda resource, ns, errors, **k: items.get(resource, []))
    comps = {
        "deployment/front": {
            **_comp("front", "2026-10-19T07:50:00Z"),
            "services": [{"name": "front", "ready_endpoints": 0}],
        }
    }
    found = outside.collect_outside([NS], comps, {}, [], [])
    ing = next(f for f in found if f["kind"] == "Ingress")
    assert ing["components"] == ["deployment/front"] and any("0 ready endpoints" in s for s in ing["signals"])
    pc = next(f for f in found if f["kind"] == "PriorityClass")
    assert pc["components"] == ["deployment/front"] and "globalDefault True" in pc["signals"][0]


def test_namespaces_the_app_addresses_are_read_too(monkeypatch):
    from clients.jev_diag.v2 import outside

    dep = {
        "spec": {
            "template": {
                "spec": {
                    "containers": [
                        {
                            "env": [
                                {"name": "DB", "value": "mysql://tidb.tidb-cluster.svc:4000/app"},
                                {"name": "OTEL", "value": "http://otel-collector.observe.svc:4317"},
                            ]
                        }
                    ]
                }
            }
        }
    }
    monkeypatch.setattr(
        outside, "kubectl_items", lambda resource, ns, errors, **k: [dep] if resource == "deployments" else []
    )
    assert outside.related_namespaces([NS], []) == ["tidb-cluster"]


def test_a_revision_made_while_deploying_is_not_a_change():
    from clients.jev_diag.v2 import changes

    meta = {"creationTimestamp": "2026-10-19T05:34:07Z"}
    assert changes.object_change("d/x", "Deployment", "x", NS, meta, revised_at="2026-10-19T05:34:10Z") is None
    later = changes.object_change("d/x", "Deployment", "x", NS, meta, revised_at="2026-10-19T05:34:40Z")
    assert later is None or later.get("revision_changed")  # None only when outside the change horizon


def test_one_change_on_several_workloads_is_reported_as_one_cause():
    from clients.jev_diag.v2.report import build_report

    def db(name: str) -> dict:
        return {
            **_comp(name, "t"),
            "evidence_kinds": ["change", "symptoms"],
            "template_changes": {
                "changed_at": "2026-10-19T07:58:38Z",
                "changes": [f"container hotel-reserv-{name}-c image: mongo:4.4.6 -> mongo:8.0.14-rc0"],
            },
        }

    comps = {f"deployment/{n}": db(n) for n in ("mongodb-geo", "mongodb-rate")}
    comps["deployment/rate"] = {**_comp("rate", "t"), "signals": ["container waiting in CrashLoopBackOff"]}
    text = build_report(_snap(comps), _diag("deployment/mongodb-geo", "image: mongo:4.4.6 -> mongo:8.0.14-rc0"))
    assert text.startswith(
        "Root cause: Deployments `mongodb-geo`, `mongodb-rate` in namespace `shop` (the same change on 2)"
    )


class _Answers:
    """A fake decision model: triage picks from a script; each investigated candidate gets its scripted origin
    probability; every other question takes its first option."""

    def __init__(self, triage: list[dict[str, float]], origin: dict[str, float], picks: dict | None = None):
        self.triage, self.origin, self.picks, self.model = list(triage), origin, picks or {}, "fake"

    def system_one(self, state, questions, **kwargs):
        from typesafe_sdk import Choice, ChoiceAnswer, NoulAnswer

        from clients.jev_diag.v2.classifier import COMPONENT_QUESTION

        answers = {}
        for name, q in questions.items():
            if name == COMPONENT_QUESTION:
                probs = self.triage.pop(0)
            elif name == "verdict":
                p = self.origin.get(state["candidate"]["id"], 0.1)
                probs = {"origin": p, "victim": 1 - p, "unrelated": 0.0, "undetermined": 0.0}
            elif isinstance(q, Choice):
                opts = list(q.criteria)
                pick = self.picks.get(name, opts[0])
                probs = {o: (1.0 if o == pick else 0.0) for o in opts}
            else:
                answers[name] = NoulAnswer(noul=0.1)
                continue
            best = max(probs, key=probs.get)
            answers[name] = ChoiceAnswer(choice=best, confidence=probs[best], probabilities=probs)

        from types import SimpleNamespace

        choices = {k: v for k, v in answers.items() if isinstance(v, ChoiceAnswer)}
        nouls = {k: v for k, v in answers.items() if isinstance(v, NoulAnswer)}
        return SimpleNamespace(model="fake", usage=None, choices=choices, nouls=nouls, answers=dict(answers))


def test_when_the_two_triage_asks_disagree_both_picks_are_examined_and_the_stronger_origin_wins():
    from clients.jev_diag.v2.tree import IterativeDiagnoser

    comps = {f"deployment/{n}": {**_comp(n, "t"), "signals": ["x"], "healthy": False} for n in ("a", "b", "c")}
    triage = [
        {"deployment/a": 0.6, "deployment/b": 0.3, "deployment/c": 0.1, "other": 0.0},
        {"deployment/a": 0.3, "deployment/b": 0.6, "deployment/c": 0.1, "other": 0.0},
    ]
    client = _Answers(triage, {"deployment/a": 0.65, "deployment/b": 0.95})
    d = IterativeDiagnoser(client, fetch_detail=lambda s, c: {}).diagnose(_snap(comps))
    assert d.component == "deployment/b"
    examined = [s["component"] for s in d.investigation["steps"] if s.get("component")]
    assert set(examined[:2]) == {"deployment/a", "deployment/b"}


def test_telemetry_lines_come_from_the_team_tools_and_skip_empty_results(monkeypatch):
    from types import SimpleNamespace

    from clients.jev_agent import metrics
    from clients.jev_agent.tools import call_edges
    from clients.jev_diag.v2 import telemetry

    monkeypatch.setattr(telemetry, "_available", True)
    monkeypatch.setenv("JEV_DIAG_V2_TELEMETRY", "1")
    by_family = {"cpu": ["cpu 0.95 cores of a 1.0 limit (95%)"], "memory": ["no memory series found for x"], "rpc": []}
    monkeypatch.setattr(metrics, "metric_lines", lambda ctx, cid, fam: SimpleNamespace(lines=by_family[fam]))
    monkeypatch.setattr(
        call_edges, "run_call_edges", lambda ctx, cid: SimpleNamespace(lines=["frontend -> cart 40% errors"])
    )
    lines = telemetry.component_lines(_snap({"deployment/cart": _comp("cart", "t")}), "deployment/cart")
    assert lines == ["cpu metrics: cpu 0.95 cores of a 1.0 limit (95%)", "observed calls: frontend -> cart 40% errors"]
    monkeypatch.setenv("JEV_DIAG_V2_TELEMETRY", "0")
    assert telemetry.component_lines(_snap({}), "deployment/cart") == []


def test_a_caller_retrying_into_a_capped_callee_is_reported_as_the_call_path():
    from clients.jev_diag.v2 import paths, report

    line = "b-svc: request failed dependency=b code=DeadlineExceeded"
    caller = {
        **_comp("a", "t"),
        "log_findings": [
            {"classes": ["connectivity or timeout"], "count": 60, "targets": ["deployment/b"], "line": line}
        ],
        "_call_settings": {"B_RPC_TIMEOUT_MS": "750", "B_RPC_MAX_ATTEMPTS": "3", "C_RPC_TIMEOUT_MS": "500"},
    }
    callee = {**_comp("b", "t"), "_call_settings": {"B_BACKEND_QPS_LIMIT": "20"}}
    comps = {"deployment/a": caller, "deployment/b": callee, "deployment/c": _comp("c", "t")}
    found = paths.call_paths(comps)
    assert found[0]["caller_settings"] == ["B_RPC_TIMEOUT_MS=750", "B_RPC_MAX_ATTEMPTS=3"]
    assert found[0]["feedback_loop"] and comps["deployment/b"]["call_paths"]
    snap = _snap(comps)
    snap.cluster["call_paths"] = found
    text = report.build_report(snap, _diag("deployment/b"))
    assert text.startswith("Root cause: the call path from Deployment `a` to Deployment `b` in namespace `shop`")
    assert "B_BACKEND_QPS_LIMIT=20" in text and "DeadlineExceeded" in text
    callee["signals"] = ["pod b-1: restarted 3 times"]  # a callee failing on its own is not a call-path fault
    assert paths.call_paths(comps) == []
    cm = paths.setting_pairs({"rpc.yaml": "timeout: 50ms\nretries: 30\nname: x", "LOG_LEVEL": "info"})
    assert cm == {"rpc.yaml:timeout": "50ms", "rpc.yaml:retries": "30"}


def _two_faults(link: bool):
    comps = {
        "deployment/a": {**_comp("a", "t"), "signals": ["s"], "evidence_kinds": ["change", "symptoms"]},
        "deployment/b": {**_comp("b", "t"), "signals": ["s"], "evidence_kinds": ["configuration", "symptoms"]},
        "deployment/c": _comp("c", "t"),
    }
    if link:
        comps["deployment/b"]["errors_point_to"] = ["deployment/a"]
    return _snap(comps)


def test_failures_the_cause_does_not_explain_can_add_a_second_independent_cause():
    from clients.jev_diag.v2.tree import IterativeDiagnoser

    triage = [{"deployment/a": 0.8, "deployment/b": 0.15, "deployment/c": 0.05, "other": 0.0}] * 2
    origin = {"deployment/a": 0.95, "deployment/b": 0.9}
    client = _Answers(triage, origin, picks={"fault_start": "both"})
    d = IterativeDiagnoser(client, fetch_detail=lambda s, c: {}).diagnose(_two_faults(link=False))
    assert d.component == "deployment/a" and d.second.component == "deployment/b"
    assert "A second, independent root cause:\nRoot cause: Deployment `b`" in d.text
    # b's errors name a: a explains b, so b is never offered as a second cause
    client = _Answers(triage, origin, picks={"fault_start": "both"})
    d = IterativeDiagnoser(client, fetch_detail=lambda s, c: {}).diagnose(_two_faults(link=True))
    assert d.second is None and [s["component"] for s in d.investigation["steps"]] == ["deployment/a"]


def test_report_states_only_what_the_facts_show_for_a_job_that_never_completes():
    from clients.jev_diag.v2.report import build_report

    comp = {
        **_comp("archiver", "t"),
        "kind": "CronJob",
        "signals": [
            "4 jobs of this cronjob are active without completing (they pile up)",
            "pod a-1: container main completed but container side keeps running, so this job pod never completes",
        ],
    }
    text = build_report(_snap({"cronjob/archiver": comp}), _diag("cronjob/archiver", "container side command: nc -lk"))
    assert "Mechanism: pod a-1: container main completed but container side keeps running" in text
    assert "Running and Ready" not in text and text.count("nc -lk") == 1
    assert "Effect: 4 jobs of this cronjob are active without completing (they pile up)." in text


def test_a_job_driving_a_failing_component_and_a_topic_input_are_named_as_the_broken_things():
    import base64

    from clients.jev_diag.v2 import checks
    from clients.jev_diag.v2.report import build_report

    script = base64.b64encode(b"import redis\nc = redis.Redis(host='store-b', port=6379)\nc.set('k', 'x' * 1000000)")
    assert "store-b" in checks.command_host_candidates(f"exec(base64.b64decode('{script.decode()}'))")
    job = {**_comp("filler", "t"), "kind": "Job", "signals": ["job has 1 active pod(s) and has not completed"]}
    job["_calls"] = ["deployment/store-b"]
    job["containers"] = [{"name": "f", "command": ["python3", "-c", f"exec(base64.b64decode('{script.decode()}'))"]}]
    store = {
        **_comp("store-b", "t"),
        "evidence_kinds": ["symptoms"],
        "services": [{"name": "store-b"}],
        "signals": ["pod s-1: container store-b was OOMKilled", "only 0 of 1 desired replicas are ready"],
    }
    text = build_report(_snap({"job/filler": job, "deployment/store-b": store}), _diag("job/filler", "command"))
    assert text.startswith(
        "Root cause: Service `store-b` (Deployment `store-b`) in namespace `shop`, driven by Job `filler`, which calls "
        "it: pod s-1: container store-b was OOMKilled."
    )
    assert "decoded from base64" in text and "x' * 1000000" in text
    broker = {**_comp("queue-a", "t"), "containers": [{"image": "apache/kafka:3.7"}]}
    consumer = {**_comp("reader", "t"), "signals": []}
    d = _diag("deployment/reader", blocked_input={"source": "logs", "input": "env IN_TOPIC=events-x"})
    text = build_report(_snap({"deployment/reader": consumer, "deployment/queue-a": broker}), d)
    assert text.startswith("Root cause: the Kafka topic `events-x` (named by env `IN_TOPIC`) consumed by Deployment")
