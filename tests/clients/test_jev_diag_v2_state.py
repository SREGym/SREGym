"""jev_diag v2: the state the model reads. Budget fitting cuts the least informative content first, log lines are
counted by what they say and shown with their cause, and a failed read is never reported as an absence."""

from __future__ import annotations

import json

from clients.jev_diag.timeutil import parse_time
from clients.jev_diag.v2 import collector

NOW = parse_time("2026-01-05T10:00:00Z")


def _healthy(name: str, pad: int = 40) -> dict:
    return {
        "kind": "Deployment",
        "name": name,
        "namespace": "ns",
        "replicas": {"desired": 2, "ready": 2, "available": 2, "updated": 2},
        "containers": [
            {"name": name, "image": f"registry.local/{name}:1.0", "env_names": [f"VAR_{i}" for i in range(pad)]}
        ],
        "pods": [{"name": f"{name}-pod-{i}", "phase": "Running", "ready": "1/1", "node": "node-a"} for i in range(4)],
        "services": [{"name": name, "ports": ["80->8080/TCP"], "ready_endpoints": 2}],
        "log_signals": [{"count": 3, "line": f"{name}: old warning {i}", "state": "stopped"} for i in range(3)],
        "signals": [],
        "healthy": True,
    }


def _failing(name: str, logs: int = 4) -> dict:
    return {
        "kind": "Deployment",
        "name": name,
        "namespace": "ns",
        "replicas": {"desired": 1, "ready": 0},
        "containers": [{"name": name, "image": f"registry.local/{name}:2.0", "command": ["serve", "--upstream=store"]}],
        "pods": [{"name": f"{name}-pod", "phase": "Running", "ready": "0/1"}],
        "services": [{"name": name, "ports": ["80->8080/TCP"], "ready_endpoints": 0}],
        "log_signals": [{"count": 9, "line": f"{name}: request failed ({i})", "state": "ongoing"} for i in range(logs)],
        "signals": ["only 0 of 1 desired replicas are ready"],
        "healthy": False,
    }


def _state(components: dict, **cluster) -> dict:
    return {"application": {"name": "app"}, "cluster": cluster, "components": components}


# --------------------------------------------------------------------------- budget fitting: rank, then cap


def test_healthy_components_become_roster_entries_before_any_symptomatic_fact_is_cut():
    comps = {f"deployment/svc-{i}": _healthy(f"svc-{i}") for i in range(12)}
    comps["deployment/svc-0"]["clients"] = ["deployment/api", "deployment/svc-1"]
    comps["deployment/svc-1"]["calls_services"] = ["ns/api", "ns/svc-2"]
    comps["deployment/api"] = _failing("api")
    state = _state(comps)
    budget = collector.estimate_tokens(state) // 3
    fitted, applied = collector.fit_state_to_budget(state, budget)
    assert collector.estimate_tokens(fitted) <= budget
    assert fitted["components"]["deployment/api"] == comps["deployment/api"]  # untouched
    assert fitted["components"]["deployment/svc-5"] == {"kind": "Deployment", "ready": "2/2", "healthy": True}
    # a healthy component keeps only its links to symptomatic components
    assert fitted["components"]["deployment/svc-0"]["clients"] == ["deployment/api"]
    assert fitted["components"]["deployment/svc-1"]["calls_services"] == ["ns/api"]
    assert not any(label.startswith("all:") for label in applied)


def test_trimming_never_adds_fields_and_rule_bodies_are_removable():
    comps = {f"deployment/svc-{i}": _healthy(f"svc-{i}") for i in range(3)}
    comps["deployment/api"] = _failing("api")
    comps["deployment/api"]["network_policies"] = [
        {"name": "limit", "effect": "restricts ingress to 1 rule(s)", "ingress_rules": [{"from": [{"x": "y" * 400}]}]}
    ]
    state = _state(comps)
    fitted, applied = collector.fit_state_to_budget(state, 300)
    assert "all: drop network policy rule bodies" in applied
    policy = fitted["components"]["deployment/api"]["network_policies"][0]
    assert "ingress_rules" not in policy and policy["effect"]
    assert all("previous_container_logs" not in c for c in fitted["components"].values())


def test_cluster_lists_keep_what_acts_on_symptomatic_components_and_name_the_rest():
    comps = {"deployment/api": _failing("api"), "deployment/idle": _healthy("idle")}
    findings = [
        {"kind": "Lease", "name": f"lease-{i}", "signal": "written after deploy " + "z" * 300} for i in range(30)
    ]
    findings.append(
        {"kind": "Role", "name": "api-reader", "signal": "denies list pods", "components": ["deployment/api"]}
    )
    state = _state(comps, findings=findings)
    fitted, applied = collector.fit_state_to_budget(state, collector.estimate_tokens(state) // 2)
    kept = fitted["cluster"]["findings"]
    assert len(kept) == collector.MAX_DETAILED_CLUSTER_ENTRIES and "api-reader" in {f["name"] for f in kept}
    others = fitted["cluster"]["other_findings"]["Lease"]
    assert len(others) + len(kept) == len(findings)  # every object is still named
    assert fitted["components"]["deployment/api"]["log_signals"] == comps["deployment/api"]["log_signals"]


def test_repeated_facts_go_first_and_lose_nothing():
    api = _failing("api", logs=2)
    api["log_findings"] = [
        {"classes": ["connectivity or timeout"], "count": 9, "targets": [], "line": "api: request failed (0)"}
    ]
    outside = [
        {"kind": "Webhook", "name": "check", "signals": ["backend has 0 ready endpoints"]},
        {"kind": "Webhook", "name": "other", "signals": ["backend has 0 ready endpoints"], "routes": ["a"]},
    ]
    findings = [{"kind": "Webhook", "name": "check", "signal": "backend has 0 ready endpoints; created after deploy"}]
    state = _state({"deployment/api": api}, findings=findings, outside_objects=outside)
    budget = collector.estimate_tokens(state) - collector.estimate_tokens(outside[0]) - 5  # both dedupes needed
    fitted, applied = collector.fit_state_to_budget(state, budget)
    assert [label.split(":")[0] for label in applied] == ["dedupe", "dedupe"]
    assert fitted["cluster"].get("outside_objects", [])[-1:] == outside[1:]  # it says more than the findings
    signature = fitted["components"]["deployment/api"]["log_signals"][0]
    assert (
        signature["classes"] == ["connectivity or timeout"]
        and "log_findings" not in fitted["components"]["deployment/api"]
    )


def test_symptomatic_logs_are_cut_only_after_every_other_reduction():
    labels = [label for label, _ in collector._trim_steps()]
    first_log_cut = min(i for i, label in enumerate(labels) if "log" in label and not label.startswith("dedupe"))
    for step in ("healthy: roster", "cluster: findings", "all: drop resource_usage", "all: pods to 1"):
        assert next(i for i, label in enumerate(labels) if label.startswith(step)) < first_log_cut, step
    comps = {f"deployment/svc-{i}": _healthy(f"svc-{i}", pad=200) for i in range(20)}
    comps["deployment/api"] = _failing("api", logs=8)
    state = _state(comps, findings=[{"kind": "Lease", "name": f"l{i}", "signal": "x" * 200} for i in range(40)])
    fitted, applied = collector.fit_state_to_budget(state, collector.estimate_tokens(state) // 4)
    assert fitted["components"]["deployment/api"]["log_signals"] == comps["deployment/api"]["log_signals"]


def test_components_with_an_abnormality_come_first_in_the_state():
    comps = {"deployment/a": _healthy("a"), "deployment/b": _failing("b"), "deployment/c": _healthy("c")}
    comps["deployment/d"] = _failing("d")
    snap = collector.ClusterSnapshot(app={}, components=comps, cluster={})
    assert list(snap.to_state()["components"]) == ["deployment/b", "deployment/d", "deployment/a", "deployment/c"]
    assert list(snap.components) == ["deployment/a", "deployment/b", "deployment/c", "deployment/d"]


# --------------------------------------------------------------------------- log lines


def _line(stamp: str, text: str) -> str:
    return f"[pod/api-7c9d/app] {stamp} {text}"


def test_one_message_is_one_signature_whatever_its_timestamps_counters_durations_and_ids():
    lines = [
        _line(
            "2026-01-05T09:59:01.1Z",
            "ERROR job 3f9a1c7e2b: attempt 1 of 5 failed at 2026-01-05 09:59:01,505 after 850ms",
        ),
        _line(
            "2026-01-05T09:59:02.1Z",
            "ERROR job 7d02be9a41: attempt 2 of 5 failed at 2026-01-05 09:59:02,117 after 1.5s",
        ),
        _line("2026-01-05T09:59:03.1Z", "ERROR job a1b2c3: attempt 3 of 5 failed at 2026-01-05 09:59:03,004 after 2s"),
    ]
    signals = collector.extract_log_signals(lines, at=NOW)
    assert len(signals) == 1 and signals[0]["count"] == 3
    assert signals[0]["line"] == "app: " + collector.log_text(lines[0])[1]  # the first line, as written
    for first, later in (
        ("[2026-Jan-05 09:59:03.004] <error>: lost lock", "[2026-Jan-05 10:01:17.993] <error>: lost lock"),
        ("Mon Jan  5 09:59:04 2026 lost lock", "Mon Jan  5 10:00:04 2026 lost lock"),
        ("I0105 09:59:04.123456 1 lock.go:42] lost lock 17", "I0105 10:02:44.000001 1 lock.go:42] lost lock 18"),
        (
            "lost lock held by 8a6e0804-2bd0-4672-b79d-d97027f9071a",
            "lost lock held by 4c4ccd6a-14b8-42c5-a4f8-9a01e8e2d3b1",
        ),
    ):
        assert collector.message_signature(first) == collector.message_signature(later)
    assert collector.message_signature("lost lock") != collector.message_signature("lost lease")


def test_a_structured_line_is_shown_message_and_error_first_without_timestamps():
    record = {
        "ts": "2026-01-05T09:59:01Z",
        "level": "error",
        "caller": "store/client.go:88",
        "context": {"request": "r" * 300},
        "msg": "query rejected",
        "error": {"code": 13, "detail": "not authorized on orders to run find"},
    }
    line = _line("2026-01-05T09:59:01.1Z", json.dumps(record))
    shown = collector.extract_log_signals([line], at=NOW)[0]["line"]
    assert shown.startswith("app: query rejected; code=13; detail=not authorized on orders to run find; level=error")
    assert "2026-01-05" not in shown and len(shown) <= collector.MAX_MESSAGE_CHARS
    logfmt = (
        'time="2026-01-05T09:59:01Z" level=error component=sync msg="sync failed" err="lease expired" ' + "k=v " * 80
    )
    assert collector.readable_log_text(logfmt).startswith("sync failed; err=lease expired; level=error; component=sync")
    assert collector.readable_log_text("ERROR paused at offset=12") == "ERROR paused at offset=12"


def test_long_messages_keep_the_cause_at_the_end_of_an_error_chain():
    chain = "create sandbox: rpc error: desc = setup network for " + "x" * 300 + ": request address: pool exhausted"
    shown = collector.trim(chain, 120)
    assert (
        len(shown) <= 120 and shown.startswith("create sandbox") and shown.endswith("request address: pool exhausted")
    )
    sentences = "0/3 nodes are available: 3 node(s) had a conflict " + "y" * 200 + ". next step: 3 not helpful."
    assert collector.trim(sentences, 120).endswith("…")  # several sentences are no chain: the head is kept
    assert collector.trim("short message", 120) == "short message"


def test_the_same_event_about_other_pods_is_one_record_and_one_signal():
    def event(pod: str, sandbox: str, minute: int) -> dict:
        return {
            "type": "Warning",
            "reason": "FailedCreatePodSandBox",
            "count": 2,
            "message": f"(combined from similar events): Failed to create pod sandbox: setup network for sandbox "
            f'"{sandbox}": plugin failed (add): pool exhausted',
            "firstTimestamp": f"2026-01-05T09:{minute:02d}:00Z",
            "lastTimestamp": f"2026-01-05T09:{minute:02d}:30Z",
            "involvedObject": {"kind": "Pod", "name": pod, "namespace": "ns"},
        }

    comps = {"deployment/api": {**_failing("api"), "warning_events": [], "signals": []}}
    events = [event("api-5f7b9c-x1k2p", "9c1e" * 16, 10), event("api-5f7b9c-q8w3z", "07ab" * 16, 20)]
    collector.attach_warning_events(events, comps, [])
    comp = comps["deployment/api"]
    assert len(comp["warning_events"]) == 1
    record = comp["warning_events"][0]
    # counted over both pods; dated by the newest series, so an older pod's events do not date the current episode
    assert record["count"] == 4 and record["first_seen"] == "2026-01-05T09:20:00Z"
    assert not record["message"].startswith("(combined")
    assert len([s for s in comp["signals"] if s.startswith("pod network setup fails")]) == 1
    assert "warning events: FailedCreatePodSandBox x4" in comp["signals"]


# --------------------------------------------------------------------------- failed reads


def test_a_reference_whose_namespace_could_not_be_read_is_not_verified_rather_than_absent():
    def comp() -> dict:
        return {"namespace": "ns", "signals": [], "config_refs": {"configmaps": ["settings"], "secrets": ["token"]}}

    unread, read = comp(), comp()
    collector.attach_config_objects({}, {"deployment/api": unread}, unread={("ConfigMap", "ns"), ("Secret", "ns")})
    assert unread["signals"] == []
    assert unread["config_objects"]["configmap/settings"] == {
        "not_verified": "the read of ConfigMaps in namespace ns failed"
    }
    collector.attach_config_objects({}, {"deployment/api": read})
    assert "referenced ConfigMap settings does not exist in namespace ns" in read["signals"]


def test_a_platform_event_signal_keeps_the_cause_at_the_end_of_a_long_message():
    from clients.jev_diag.v2.platform_events import event_signal

    message = (
        "Failed to create pod sandbox: rpc error: code = Unknown desc = failed to setup network for sandbox "
        + "x" * 300
        + ': plugin type="cni" failed (add): no free address blocks left'
    )
    signal = event_signal("FailedCreatePodSandBox", message)
    assert signal.endswith("no free address blocks left") and "…" in signal


def test_a_webhook_url_with_a_timeout_parameter_is_not_a_deadline_error():
    from clients.jev_diag.v2.checks import admission_chains

    webhook = {
        "webhook": "check.example",
        "configuration": "check",
        "kind": "ValidatingWebhookConfiguration",
        "timeout_seconds": 5,
        "_rules": [{"operations": ["CREATE"], "resources": ["pods"], "apiGroups": [""], "apiVersions": ["v1"]}],
    }

    def comp(message: str) -> dict:
        event = {"reason": "FailedCreate", "message": message}
        return {"namespace": "ns", "warning_events": [event]}

    url = 'Post "https://check.ns.svc:443/validate?timeout=5s"'
    certificate = {
        "deployment/a": comp(f"failed calling webhook: {url}: x509: certificate signed by unknown authority")
    }
    assert admission_chains([webhook], ["ns"], {}, certificate) == []
    deadline = {"deployment/a": comp(f"failed calling webhook: {url}: context deadline exceeded")}
    (finding,) = admission_chains([webhook], ["ns"], {}, deadline)
    assert finding["components"] == ["deployment/a"]


def test_endpoints_that_could_not_be_read_are_unknown_not_zero():
    from clients.jev_diag.v2.collector import attach_services

    def comps():
        return {
            "deployment/web": {
                "namespace": "ns",
                "kind": "Deployment",
                "template_labels": {"app": "web"},
                "containers": [],
                "services": [],
                "signals": [],
            }
        }

    service = {"metadata": {"namespace": "ns", "name": "web"}, "spec": {"selector": {"app": "web"}, "ports": []}}
    unread = comps()
    attach_services([service], None, unread)
    assert unread["deployment/web"]["signals"] == []
    assert unread["deployment/web"]["services"][0]["ready_endpoints"] is None
    empty = comps()
    attach_services([service], [], empty)
    assert empty["deployment/web"]["signals"] == ["service web selects this component but has 0 ready endpoints"]


def test_a_signature_that_returns_after_a_silence_dates_its_episode_from_the_return():
    from datetime import UTC, datetime, timedelta

    at = datetime(2026, 1, 5, 10, 0, tzinfo=UTC)
    warmup = [at - timedelta(seconds=600 - 5 * i) for i in range(4)]  # four lines at start-up, then quiet
    current = [at - timedelta(seconds=60 - 5 * i) for i in range(12)]  # back a minute ago, still going
    timing = collector.signature_recency(warmup + current, at)
    assert timing["state"] == "ongoing" and timing["first_seen_seconds_ago"] == 600
    assert timing["episode_start_seconds_ago"] == 60
    assert "episode_start_seconds_ago" not in collector.signature_recency(current, at)  # one episode: first sighting


def test_a_long_message_gives_up_its_ids_before_its_words():
    sandbox = "58e3562fc4a02c9e78f90c1f8178f9295c35105973bd44520e9b8efbec366021"
    message = (
        f'Failed to create pod sandbox: failed to setup network for sandbox "{sandbox}": plugin failed (add): '
        "failed to request addresses: assigned 0 of 1 requested addresses, no more free address blocks"
    )
    shown = collector.trim(message, 200)
    assert sandbox not in shown and shown.endswith("no more free address blocks")
    assert collector.trim(f"short {sandbox}", 200) == f"short {sandbox}"  # a message that fits is shown as written
