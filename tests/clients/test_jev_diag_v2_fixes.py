"""jev_diag v2: evidence that must not be lost or misread (collection, change records, evidence kinds)."""

from __future__ import annotations

from clients.jev_diag.timeutil import parse_time
from clients.jev_diag.v2 import changes, checks, collector, derive, investigate


def test_telemetry_names_are_whole_tokens():
    assert derive.OBSERVABILITY_RE.search("otel-collector")
    assert derive.OBSERVABILITY_RE.search("prometheus-server")
    assert not derive.OBSERVABILITY_RE.search("motel-booking")
    assert not derive.OBSERVABILITY_RE.search("temporal-worker")


def test_a_late_change_in_a_long_env_value_is_reported_where_it_differs():
    base = "jdbc:postgresql://db:5432/orders?ssl=true&connectTimeout=10&socketTimeout=30&applicationName=orders"
    old = {"containers": [{"name": "app", "env": [{"name": "DSN", "value": base}]}]}
    new = {"containers": [{"name": "app", "env": [{"name": "DSN", "value": base.replace("orders", "billing")}]}]}
    diff = changes.template_diff({"spec": old}, {"spec": new})
    assert any("env DSN:" in d and "billing" in d for d in diff), diff


def test_a_scale_by_an_operator_is_a_change_and_by_an_autoscaler_is_not():
    meta = {
        "managedFields": [
            {"manager": "kubectl", "subresource": "scale", "time": "2026-10-19T08:00:00Z"},
            {"manager": "kube-controller-manager", "subresource": "scale", "time": "2026-10-19T08:01:00Z"},
            {"manager": "kubelet", "subresource": "status", "time": "2026-10-19T08:02:00Z"},
        ]
    }
    assert [w["manager"] for w in changes.spec_writes(meta)] == ["kubectl"]


def test_no_rollout_deadlock_is_claimed_for_zero_replicas():
    def workload(replicas: int) -> dict:
        return {
            "kind": "Deployment",
            "metadata": {"name": "x", "namespace": "ns"},
            "spec": {
                "replicas": replicas,
                "strategy": {"type": "RollingUpdate", "rollingUpdate": {"maxUnavailable": "25%", "maxSurge": "25%"}},
                "selector": {"matchLabels": {"app": "x"}},
                "template": {
                    "metadata": {"labels": {"app": "x"}},
                    "spec": {"containers": [{"name": "x", "image": "i"}]},
                },
            },
            "status": {},
        }

    for replicas in (0, 1, 4):  # 25% surge rounds up, so a rollout always progresses
        signals = collector.summarize_workload(workload(replicas))["signals"]
        assert not any("can never progress" in s for s in signals), (replicas, signals)


def test_error_lines_include_resource_exhaustion_and_certificate_failures():
    for line in (
        "java.lang.OutOfMemoryError: Java heap space",
        "listen tcp :8080: bind: address already in use",
        "write /data/x: no space left on device",
        "accept4: too many open files",
        "Get https://cart: x509: certificate has expired",
    ):
        assert collector.is_error_line(line), line


def test_a_crash_keeps_its_message_when_a_stack_trace_follows():
    lines = ["starting", "panic: no backend available", "", *[f"\tmain.go:{i}" for i in range(30)], "exit status 2"]
    excerpt = collector.crash_excerpt([ln for ln in lines if ln], 12)
    assert excerpt[-1] == "earlier: panic: no backend available"
    assert len(excerpt) == 12


def test_an_investigation_sees_the_warning_events_attributed_to_the_workload():
    comp = {
        "warning_events": [
            {
                "reason": "FailedCreate",
                "object": "replicaset/web-7d9f",
                "message": 'pods "web-7d9f-x" is forbidden: violates PodSecurity "restricted:latest"',
                "last_seen": "2026-10-19T08:05:00Z",
                "count": 9,
            }
        ]
    }
    events = investigate.merged_events([], comp)
    assert events[0]["reason"] == "FailedCreate" and events[0]["type"] == "Warning"


def test_preemption_reported_as_a_normal_event_reaches_the_workload():
    comps = {
        "deployment/web": {"kind": "Deployment", "name": "web", "namespace": "ns", "signals": [], "warning_events": []}
    }
    preempted = {
        "type": "Normal",
        "reason": "Preempted",
        "message": "Preempted by pod 1234 on node worker2",
        "involvedObject": {"kind": "Pod", "name": "web-7d9f6c8b5-abcde", "namespace": "ns"},
        "lastTimestamp": "2026-10-19T08:05:00Z",
    }
    collector.attach_warning_events([preempted], comps, [])
    assert comps["deployment/web"]["signals"][0].startswith("pods of this workload were preempted")


def test_symptom_onset_ignores_what_predates_the_application_and_foreign_alerts():
    comps = {
        "deployment/web": {
            "namespace": "ns",
            "created": "2026-10-19T08:00:00Z",
            "warning_events": [
                {"reason": "BackOff", "first_seen": "2026-10-19T08:06:00Z", "last_seen": "2026-10-19T08:09:40Z"}
            ],
        }
    }
    cluster = {
        "firing_alerts": [
            {"alertname": "Old", "active_since": "2026-10-19T07:00:00Z", "labels": {"namespace": "ns"}},
            {"alertname": "Elsewhere", "active_since": "2026-10-19T08:02:00Z", "labels": {"namespace": "other"}},
        ]
    }
    moment, source = changes.estimate_symptom_onset(comps, cluster, parse_time("2026-10-19T08:10:00Z"))
    assert source == "BackOff events on deployment/web" and moment == parse_time("2026-10-19T08:06:00Z")


def test_a_network_policy_selecting_by_expression_selects_its_pods():
    policy = {
        "metadata": {"name": "deny", "namespace": "ns"},
        "spec": {
            "podSelector": {"matchExpressions": [{"key": "app", "operator": "In", "values": ["web"]}]},
            "policyTypes": ["Ingress"],
        },
    }
    comps = {
        "deployment/web": {"namespace": "ns", "template_labels": {"app": "web"}, "network_policies": [], "signals": []},
        "deployment/db": {"namespace": "ns", "template_labels": {"app": "db"}, "network_policies": [], "signals": []},
    }
    summaries = collector.attach_network_policies([policy], comps)
    assert summaries[0]["components"] == ["deployment/web"]


def test_projected_volumes_count_as_config_references():
    obj = {
        "kind": "Deployment",
        "metadata": {"name": "x", "namespace": "ns"},
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": {"app": "x"}},
            "template": {
                "metadata": {"labels": {"app": "x"}},
                "spec": {
                    "containers": [{"name": "x", "image": "i"}],
                    "volumes": [{"name": "cfg", "projected": {"sources": [{"configMap": {"name": "x-config"}}]}}],
                },
            },
        },
        "status": {},
    }
    assert collector.summarize_workload(obj)["config_refs"]["configmaps"] == ["x-config"]


def test_quoted_text_in_a_pointer_signal_is_not_a_change_and_alerts_are_symptoms():
    comps = {
        "deployment/web": {
            "signals": [
                "errors logged by 1 other component(s) (api) name this component (connectivity), e.g. 'session created '",
                "firing alert KubePodNotReady",
                "service web selects this component but has 0 ready endpoints",
            ]
        }
    }
    derive.classify_evidence(comps)
    assert comps["deployment/web"]["evidence_kinds"] == ["pointed_at", "symptoms"]


def test_label_selector_semantics_cover_expressions():
    assert checks.label_selector_matches({"matchExpressions": [{"key": "a", "operator": "Exists"}]}, {"a": "1"})


def _meta_with_later_write(fields: dict, applied: dict | None = None) -> dict:
    import json

    meta = {
        "creationTimestamp": "2026-10-19T08:00:00Z",
        "managedFields": [
            {"manager": "helm", "operation": "Update", "time": "2026-10-19T08:00:00Z", "fieldsV1": {"f:data": {}}},
            {"manager": "kubectl-patch", "operation": "Update", "time": "2026-10-19T08:10:00Z", "fieldsV1": fields},
        ],
    }
    if applied is not None:
        meta["annotations"] = {"kubectl.kubernetes.io/last-applied-configuration": json.dumps(applied)}
    return meta


def test_a_change_states_the_value_it_set_and_the_old_one_when_kubernetes_kept_it():
    obj = {"kind": "RoutePolicy", "spec": {"nodeSelector": "zone == b", "asNumber": 64512}}
    meta = _meta_with_later_write({"f:spec": {"f:nodeSelector": {}}})
    assert changes.changed_values(obj, meta) == ["spec.nodeSelector = zone == b"]
    applied = {"spec": {"nodeSelector": "zone == a"}}
    meta = _meta_with_later_write({"f:spec": {"f:nodeSelector": {}}}, applied)
    assert changes.changed_values(obj, meta) == ["spec.nodeSelector: zone == a -> zone == b"]


def test_a_long_text_change_is_shown_where_it_differs_and_secrets_are_never_read():
    corefile = ".:53 {\n" + "    errors\n" * 40 + "    forward . /etc/resolv.conf\n}"
    obj = {
        "kind": "ConfigMap",
        "data": {"Corefile": corefile.replace("forward . /etc/resolv.conf", "forward . 8.8.8.8")},
    }
    meta = _meta_with_later_write({"f:data": {"f:Corefile": {}}}, {"data": {"Corefile": corefile}})
    (value,) = changes.changed_values(obj, meta)
    assert value.startswith("data.Corefile: …") and "8.8.8.8" in value and "/etc/resolv.conf" in value
    secret = {"kind": "Secret", "data": {"password": "c2VjcmV0"}}
    assert changes.changed_values(secret, _meta_with_later_write({"f:data": {"f:password": {}}})) == []
    assert changes.object_content(secret) == "keys password"


def test_list_items_are_found_by_name_in_a_changed_path():
    obj = {"spec": {"containers": [{"name": "app", "env": [{"name": "MODE", "value": "fast"}]}]}}
    assert changes.value_at(obj, "spec.containers[app].env[MODE].value") == "fast"
    assert changes.value_at(obj, "spec.containers[other].image") is changes._MISSING


def test_an_object_created_after_the_application_states_what_it_holds():
    pc = {"kind": "PriorityClass", "value": 1000000, "globalDefault": True, "metadata": {"name": "batch-critical"}}
    from clients.jev_diag.timeutil import set_now

    meta = {"creationTimestamp": "2026-10-19T08:10:00Z", "managedFields": []}
    set_now(parse_time("2026-10-19T08:11:00Z"))  # changes older than an hour are not listed
    try:
        change = changes.object_change(
            "priorityclass/batch-critical",
            "PriorityClass",
            "batch-critical",
            None,
            meta,
            targets_created=["2026-10-19T08:00:00Z"],
            obj=pc,
        )
    finally:
        set_now(None)
    assert change and '"globalDefault": true' in change["content"] and '"value": 1000000' in change["content"]
    assert "it holds" in changes.describe_change({**change, "seconds_ago": 30})


def test_a_flag_write_lists_what_changed_on_the_failing_clients_of_its_provider_only():
    import json

    from clients.jev_diag.v2 import flags

    def flag(variant):
        return {"state": "ENABLED", "variants": {"on": True, "off": False}, "defaultVariant": variant}

    before = {"flags": {"mode-x": flag("off"), "mode-y": flag("on")}}
    after = {"flags": {"mode-x": flag("on"), "mode-y": flag("on")}}
    applied = json.dumps({"data": {"flags.json": json.dumps(before)}})
    cm = {"data": {"flags.json": json.dumps(after)}, "metadata": {"annotations": {flags.LAST_APPLIED: applied}}}
    failing = {"log_findings": [{"classes": ["connectivity or timeout"], "count": 9}]}
    comps = {
        "deployment/provider": {
            "namespace": "app",
            "signals": [],
            "config_objects": {"configmap/flags": {}},
            **failing,
        },
        "deployment/a": {"namespace": "app", "signals": [], "_calls": ["deployment/provider"], **failing},
        "deployment/b": {"namespace": "app", "signals": [], "_calls": ["deployment/provider"]},  # healthy client
        "deployment/c": {"namespace": "app", "signals": [], **failing},  # failing, but not a client
    }
    config = {("ConfigMap", "app", "flags"): {"_object": cm}}
    flags.attribute_flags(comps, config, {"configmap/app/flags"})
    (signal,) = comps["deployment/a"]["signals"]
    assert (
        "flags changed: flag mode-x: ENABLED, defaultVariant 'off' = false -> ENABLED, defaultVariant 'on' = true"
        in (signal)
    )
    assert "mode-y" not in signal  # unchanged
    assert not any(comps[c]["signals"] for c in ("deployment/provider", "deployment/b", "deployment/c"))
    derive.classify_evidence(comps)
    assert comps["deployment/a"]["evidence_kinds"] == ["change"]
    # without the previous content, the flags on now are listed as on, not as changed
    comps["deployment/a"]["signals"] = []
    config = {("ConfigMap", "app", "flags"): {"_object": {"data": cm["data"]}}}
    flags.attribute_flags(comps, config, {"configmap/app/flags"})
    assert "flags on now: flag mode-x" in comps["deployment/a"]["signals"][0]
    # a flag ConfigMap not written after deploy holds the baseline: nothing is attributed
    comps["deployment/a"]["signals"] = []
    flags.attribute_flags(comps, config, set())
    assert not comps["deployment/a"]["signals"]


def test_a_workload_outside_the_application_becomes_a_finding_and_its_own_workload_change_does_not():
    ranked = [
        {
            "object": "cronjob/ops/updater",
            "kind": "CronJob",
            "name": "updater",
            "namespace": "ops",
            "affects": [],
            "at": "2026-10-19T08:00:00Z",
            "created_after_targets_s": 300,
            "seconds_ago": 60,
        },
        {
            "object": "deployment/web",
            "kind": "Deployment",
            "name": "web",
            "namespace": "ns",
            "affects": ["deployment/web"],
            "at": "2026-10-19T08:00:00Z",
            "modified_after_creation_s": 300,
            "seconds_ago": 60,
        },
    ]
    findings = derive.cluster_findings({}, ranked, {})
    assert [(x["kind"], x["name"]) for x in findings] == [("CronJob", "updater")]


def test_a_config_object_is_a_finding_only_when_some_workload_it_acts_on_does_not_read_it():
    comps = {
        "deployment/web": {"namespace": "ns", "config_objects": {"secret/db-conn": {}}},
        "deployment/db": {"namespace": "ns", "config_objects": {}},
    }

    def change(kind, name, ns, affects):
        return {
            "object": f"{kind.lower()}/{ns}/{name}",
            "kind": kind,
            "name": name,
            "namespace": ns,
            "affects": affects,
            "at": "2026-10-19T08:00:00Z",
            "modified_after_creation_s": 300,
        }

    ranked = [
        change("Secret", "db-conn", "ns", ["deployment/web"]),  # web's own configuration
        change("ConfigMap", "coredns", "kube-system", ["deployment/web"]),  # acts on web, which does not read it
        change("Secret", "db-conn", "other", ["deployment/web"]),  # same name, another namespace
    ]
    findings = derive.cluster_findings({}, ranked, comps)
    assert [(x["kind"], x["name"], x["namespace"]) for x in findings] == [
        ("ConfigMap", "coredns", "kube-system"),
        ("Secret", "db-conn", "other"),
    ]


def test_pod_creation_failing_on_a_deadline_names_the_webhook_chain_that_intercepts_it():
    rules = [{"operations": ["CREATE"], "apiGroups": [""], "resources": ["pods"]}]
    hooks = [
        {
            "kind": "ValidatingWebhookConfiguration",
            "configuration": f"policy-{i}",
            "webhook": f"p{i}.example.io",
            "timeout_seconds": t,
            "failure_policy": "Ignore",
            "_rules": rules,
            "namespace_selector": None,
        }
        for i, t in enumerate((5, 7))
    ]
    comps = {"deployment/web": {"namespace": "ns"}, "deployment/api": {"namespace": "ns"}}
    assert checks.admission_chains(hooks, ["ns"], {}, comps) == []  # nothing fails: the chain alone is no finding
    comps["deployment/api"]["warning_events"] = [{"reason": "FailedCreate", "message": "exceeded quota: pods=2"}]
    assert checks.admission_chains(hooks, ["ns"], {}, comps) == []  # a failure of another kind
    comps["deployment/web"]["warning_events"] = [
        {"reason": "FailedCreate", "message": "Internal error occurred: context deadline exceeded"}
    ]
    (finding,) = checks.admission_chains(hooks, ["ns"], {}, comps)
    assert finding["components"] == ["deployment/web"] and "add up to 12s" in finding["signals"][0]
    assert "p0.example.io (5s, failurePolicy=Ignore)" in finding["signals"][0]


def test_a_package_managers_write_is_a_change_like_any_other():
    def change(*writes):
        meta = {
            "creationTimestamp": "2026-10-19T08:10:00Z",
            "managedFields": [{"manager": m, "operation": "Update", "time": t} for m, t in writes],
        }
        return changes.object_change(
            "x/y", "ValidatingWebhookConfiguration", "y", None, meta, targets_created=["2026-10-19T08:00:00Z"]
        )

    from clients.jev_diag.timeutil import set_now

    set_now(parse_time("2026-10-19T08:16:00Z"))  # changes older than an hour are not listed
    try:
        assert change(("kubectl-create", "2026-10-19T08:10:00Z")) is not None
        assert change(("helm", "2026-10-19T08:10:00Z")) is not None  # an upgrade or install is a change
    finally:
        set_now(None)


def _tls_secret(name: str, not_before, not_after) -> dict:
    import base64

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "shop.example")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(not_before)
        .not_valid_after(not_after)
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("shop.example")]), critical=False)
        .sign(key, hashes.SHA256())
    )
    pem = cert.public_bytes(serialization.Encoding.PEM)
    return {
        "type": "kubernetes.io/tls",
        "metadata": {"name": name, "namespace": "ns"},
        "data": {"tls.crt": base64.b64encode(pem).decode(), "tls.key": "never-read"},
    }


def test_an_expired_certificate_is_reported_from_the_public_certificate_only():
    from datetime import timedelta

    at = parse_time("2026-10-19T08:00:00Z")
    expired = _tls_secret("web-tls", at - timedelta(days=30), at - timedelta(days=1))
    problems = checks.certificate_problems(expired, at)
    assert problems == ["its certificate for shop.example expired at 2026-10-18T08:00:00+00:00"]
    valid = _tls_secret("ok-tls", at - timedelta(days=1), at + timedelta(days=30))
    assert checks.certificate_problems(valid, at) == []
    assert checks.certificate_problems({"type": "Opaque", "data": {"tls.crt": "x"}}, at) == []


def test_encoded_data_is_shown_as_its_size_so_the_settings_fit():
    import base64

    blob = base64.b64encode(bytes(range(256)) * 8).decode()
    hook = {
        "kind": "ValidatingWebhookConfiguration",
        "webhooks": [
            {
                "admissionReviewVersions": ["v1"],
                "clientConfig": {"caBundle": blob, "service": {"name": "policy", "namespace": "ops"}},
                "failurePolicy": "Fail",
                "timeoutSeconds": 10,
            }
        ],
    }
    content = changes.object_content(hook)
    assert f"<{len(blob)} characters of encoded data>" in content and blob[:40] not in content
    assert '"failurePolicy": "Fail"' in content and '"timeoutSeconds": 10' in content
    sentence = "the frontend reads its upstream address from this value and retries every request three times " * 2
    script = "import os\nimport time\nprint(os.environ['TARGET'])\n" * 4
    assert changes.readable(sentence) == sentence and changes.readable(script) == script
    assert changes.readable("-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----").startswith("<")


def test_a_read_that_defines_the_application_is_retried_before_it_counts_as_empty(monkeypatch):
    from clients.jev_diag.v2 import collector as v2c

    answers = {"deployments": [None, [{"metadata": {"name": "web"}}]], "pods": [None, None, None]}

    def flaky(resource, namespace, errors, **kwargs):
        item = answers[resource].pop(0)
        if item is None:
            errors.append(f"kubectl get {resource} timed out after 45s")
            return []
        return item

    monkeypatch.setattr(v2c, "kubectl_items", flaky)
    monkeypatch.setattr(v2c, "APPLICATION_READ_PAUSE_SECONDS", 0)
    errors: list[str] = []
    assert v2c.application_items("deployments", "ns", errors) == [{"metadata": {"name": "web"}}] and errors == []
    assert v2c.application_items("pods", "ns", errors) == [] and errors == ["kubectl get pods timed out after 45s"]


def test_a_workload_created_late_says_what_it_runs():
    comp = {
        "containers": [
            {"name": "app", "command": ["worker-b"], "args": ["--port", "8081"], "image": "repo/app:1.2@sha256:abc"},
            {"name": "wait", "init": True, "image": "busybox:1.36"},
        ]
    }
    runs = changes.workload_runs(comp)
    assert (
        runs
        == "container app: command worker-b, args --port 8081, image repo/app:1.2; container wait (init): image busybox:1.36"
    )
    change = {"object": "deployment/app", "seconds_ago": 138, "created_after_targets_s": 14, "content": runs}
    assert changes.describe_change(change, "deployment/app").startswith(
        "created 138s ago, 14s after the workloads it calls; it runs container app: command worker-b"
    )


def test_a_crash_looping_container_keeps_the_current_logs_of_every_pod(monkeypatch):
    import json

    from clients.jev_diag.v2 import investigate

    current = {
        "web-a": "2026-10-19T08:00:00Z [pod/web-a/app] ERROR first pod fails to reach db\n",
        "web-b": "2026-10-19T08:00:01Z [pod/web-b/app] ERROR second pod fails to reach db\n",
    }

    def fake(args, timeout=0):
        if "--previous" in args:
            return "panic: previous run crashed\n"
        return current[args[1]]

    monkeypatch.setattr(investigate, "run_kubectl", fake)
    pods = [
        {"name": "web-a", "containers": [{"name": "app", "restarts": 0}]},
        {"name": "web-b", "containers": [{"name": "app", "restarts": 3, "ready": False}]},
    ]
    out = investigate.log_detail(["web-a", "web-b"], "ns", pods)
    text = json.dumps(out)
    assert "first pod fails" in text and "second pod fails" in text  # both pods' current logs survive
    assert "previous run crashed" in text  # and the previous run is kept separately


def test_a_rollout_that_is_paused_or_held_back_is_a_signal():
    from clients.jev_diag.v2.collector import rollout_state

    paused = rollout_state("Deployment", {"paused": True, "replicas": 2}, {"updatedReplicas": 1})
    assert paused == ["rollout paused (spec.paused): 1 of 2 replicas run the newest pod template"]
    sts = rollout_state(
        "StatefulSet",
        {"replicas": 3, "updateStrategy": {"rollingUpdate": {"partition": 2}}},
        {"currentRevision": "db-1", "updateRevision": "db-2", "updatedReplicas": 1},
    )
    assert sts == ["rolling update incomplete (partition 2): 1 of 3 replicas run the update revision"]
    assert rollout_state("DaemonSet", {}, {"updatedNumberScheduled": 2, "desiredNumberScheduled": 3})
    assert rollout_state("Deployment", {"replicas": 2}, {"updatedReplicas": 2}) == []
    assert rollout_state("StatefulSet", {}, {"currentRevision": "db-2", "updateRevision": "db-2"}) == []


def test_pods_on_an_older_template_are_reported_with_how_that_template_differs():
    def template(aliases):
        spec = {"containers": [{"name": "app", "image": "app:1"}]}
        if aliases:
            spec["hostAliases"] = aliases
        return {"metadata": {"labels": {"app": "web"}}, "spec": spec}

    revisions = [
        {"revision": 1, "template": template([{"ip": "10.0.0.9", "hostnames": ["store"]}]), "hash": "aaa"},
        {"revision": 2, "template": template(None), "hash": "bbb"},
    ]
    comp = {"pods": [{"name": "web-aaa-1", "template_hash": "aaa"}, {"name": "web-bbb-1", "template_hash": "bbb"}]}
    (signal,) = changes.live_revision_drift(comp, revisions)
    assert signal.startswith("1 pod(s) (web-aaa-1) still run pod template revision 1, not the newest (2)")
    assert "10.0.0.9" in signal and "store" in signal
    comp["pods"] = [{"name": "web-bbb-1", "template_hash": "bbb"}]
    assert changes.live_revision_drift(comp, revisions) == []


def test_saturation_is_judged_on_its_own_scale_not_only_against_a_baseline():
    from clients.jev_diag.v2.telemetry import saturation

    throttled = {"throttled": [{"labels": {"container": "app"}, "summary": {"recent": 0.4, "baseline": 0.4}}]}
    assert saturation("cpu", throttled) == ["saturation: container app is CPU-throttled in 40% of CFS periods"]
    assert saturation("cpu", {"throttled": [{"labels": {"container": "app"}, "summary": {"recent": 0.05}}]}) == []
    near = {"working_set": [{"container": "app", "summary": {"max": 95.0}, "limit": 100.0}]}
    assert saturation("memory", near) == ["saturation: container app memory working set reached 95% of its limit"]
    assert saturation("memory", {"working_set": [{"container": "app", "summary": {"max": 50.0}, "limit": 100.0}]}) == []


def test_a_service_without_ready_endpoints_is_a_symptom_not_a_configuration_mismatch():
    from clients.jev_diag.v2.investigate import service_checks

    comp = {"services": [{"name": "web", "ports": ["80->8080/TCP"], "ready_endpoints": 0}]}
    workload = {"containers": [{"ports": [{"port": 8080}]}]}
    assert service_checks(comp, workload, [{"phase": "Running"}]) == []


def test_a_zero_quota_and_a_cluster_dns_without_pods_are_signals(monkeypatch):
    import json

    from clients.jev_diag.v2 import collector

    quota = {"metadata": {"name": "q", "namespace": "ns"}, "status": {"hard": {"pods": "0"}, "used": {"pods": "0"}}}
    (summary,) = collector.summarize_namespace_constraints([], [quota])
    assert summary["signals"] == ["quota pods: hard limit 0, so nothing of it can be created"]

    cm = {"metadata": {"name": "coredns"}, "data": {"Corefile": ".:53 {\n    forward . /etc/resolv.conf\n}\n"}}
    answers = iter([json.dumps(cm), json.dumps({"items": []})])
    monkeypatch.setattr(collector, "run_kubectl", lambda args: next(answers))
    dns = collector.summarize_cluster_dns([])
    assert "CoreDNS has no pods: cluster name resolution has no server" in dns["signals"]


def test_warning_events_are_counted_in_full_and_every_reason_is_kept_before_the_cap():
    from clients.jev_diag.v2 import collector

    def ev(reason, minute):
        return {
            "type": "Warning",
            "reason": reason,
            "message": f"{reason} on volume {'abcdefghijklmnopqrst'[minute]}",  # distinct facts, so the cap applies
            "count": 1,
            "lastTimestamp": f"2026-10-19T08:{minute:02d}:00Z",
            "involvedObject": {"kind": "Deployment", "name": "web", "namespace": "ns"},
        }

    comps = {
        "deployment/web": {"kind": "Deployment", "name": "web", "namespace": "ns", "signals": [], "warning_events": []}
    }
    events = [ev("BackOff", m) for m in range(10, 20)] + [ev("FailedMount", 1)]
    collector.attach_warning_events(events, comps, [])
    kept = comps["deployment/web"]["warning_events"]
    assert len(kept) == collector.MAX_EVENTS_PER_COMPONENT and {"BackOff", "FailedMount"} <= {e["reason"] for e in kept}
    assert comps["deployment/web"]["signals"] == ["warning events: BackOff x10, FailedMount x1"]


def test_a_rare_new_error_is_ranked_beside_the_most_frequent_ones():
    from clients.jev_diag.v2.collector import rank_signatures

    retries = [
        {"line": f"retry {i}", "count": 500 - i, "first_seen_seconds_ago": 900, "state": "ongoing"} for i in range(9)
    ]
    new = {"line": "panic: invalid config", "count": 1, "first_seen_seconds_ago": 30, "state": "ongoing"}
    old = {"line": "old failure", "count": 999, "first_seen_seconds_ago": 2000, "state": "stopped"}
    ranked = rank_signatures([*retries, new, old])
    assert ranked[0]["line"] == "retry 0" and ranked[1] is new  # the newest beside the most frequent
    assert ranked[-1] is old and len(ranked) == 11


def test_an_address_in_an_error_line_names_the_component_that_owns_it():
    comps = {
        "deployment/web": {"name": "web", "services": [], "pods": [{"ip": "10.244.1.7"}]},
        "deployment/kv": {"name": "kv", "services": [{"name": "kv", "cluster_ip": "10.96.0.5"}], "pods": []},
    }
    aliases = derive._name_aliases(comps)
    line = "dial tcp 10.96.0.5:8500: connect: connection refused"
    assert derive._mentions(line, aliases, "deployment/web") == {"deployment/kv"}
    assert derive._mentions("dial tcp 10.96.0.55:8500: refused", aliases, "deployment/web") == set()
    assert derive._mentions("request from 10.244.1.7 rejected", aliases, "deployment/kv") == {"deployment/web"}


def test_a_missing_optional_reference_is_not_reported_and_a_required_one_is():
    from clients.jev_diag.v2.collector import attach_config_objects

    comp = {
        "namespace": "ns",
        "signals": [],
        "config_refs": {"configmaps": ["extra", "main"], "secrets": [], "optional": ["configmaps/extra"]},
    }
    attach_config_objects({}, {"deployment/web": comp})
    assert comp["signals"] == ["referenced ConfigMap main does not exist in namespace ns"]


def test_error_lines_are_recognised_by_standard_shapes_not_a_list_of_past_failures():
    from clients.jev_diag.v2.collector import is_error_line

    for line in (
        "write /data/x: no space left on device",  # errno message
        "EDQUOT: Disk quota exceeded",  # an errno name never listed by hand
        "java.lang.IllegalStateException: boom",  # an exception class
        '{"level":"error","msg":"shard sync stalled"}',  # a declared level, no error word
        "x509: certificate has expired",  # a TLS library prefix
    ):
        assert is_error_line(line), line
    for line in ("FEATURE ENABLED", "listening on :8080", "processed 12 items"):
        assert not is_error_line(line), line


def test_explicit_failures_count_at_info_level_and_normal_logs_do_not():
    """v1's test, without an unlevelled "consumer paused at offset" line: a pause is a state, and a consumer that
    stays stuck shows as a static committed offset (probes.py) or as one failure repeating (repeated_failures)."""
    failures = [
        "-NOAUTH Authentication required.",
        "WRONGPASS invalid username-password pair",
        "HTTP 429 Too Many Requests",
        "rate limit exceeded, status 429",
        "ERROR partition remains paused at offset=1234",
        "ERROR record validation failed at offset=1234: invalid input",
        'level=info msg="skipping malformed record"',
        '{"level":"info","status":429,"message":"request rejected"}',
        'INFO authentication failed for user "worker"',
        "HTTP/1.1 403 Forbidden",
        "RPC returned RESOURCE_EXHAUSTED",
    ]
    normal = [
        "INFO server started on port 429",
        "INFO processed offset 1234 status=200",
        'level=info msg="request timeout configured"',
        "INFO no errors, failed=0",
        "INFO authentication required=true",
        "INFO rate limit configured to 429 requests per second",
        "INFO consumer resumed at offset 1234",
        "INFO consumer paused for rebalance",
        "INFO malformed record count=0",
        "DEBUG HTTP 429 retry policy configured",
    ]
    assert [line for line in failures if not collector.is_error_line(line)] == []
    assert [line for line in normal if collector.is_error_line(line)] == []


def test_ranked_signatures_hold_each_signature_once():
    common = {"count": 40, "first_seen_seconds_ago": 300}
    rare = {"count": 2, "first_seen_seconds_ago": 30}
    stopped = {"count": 9, "state": "stopped"}
    assert collector.rank_signatures([common]) == [common]
    ranked = collector.rank_signatures([common, rare, stopped])
    assert ranked == [common, rare, stopped]


def test_the_last_resort_trim_never_drops_an_unhealthy_component():
    comps = {f"deployment/svc-{i:02d}": {"healthy": True, "signals": [], "pad": "x" * 200} for i in range(30)}
    comps["deployment/zz-failing"] = {"healthy": False, "signals": ["only 0 of 1 desired replicas are ready"]}
    fitted, _ = collector.fit_state_to_budget({"components": comps, "cluster": {}}, 600)
    assert "deployment/zz-failing" in fitted["components"]
    assert len(fitted["components"]) < len(comps)  # healthy ones went first
