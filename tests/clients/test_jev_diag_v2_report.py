"""The v2 report states one subject with its faulty setting as field = value, what Kubernetes does with it, the fault
type, the change (old -> new), the impact the cause explains and the other party, within fixed limits and without
raw documents, encoded data, scripts or timings as facts. Neutral fixtures; each test checks a rule, not a case."""

from clients.jev_diag.v2.classifier import ChoiceResult, Diagnosis
from clients.jev_diag.v2.collector import ClusterSnapshot
from clients.jev_diag.v2.report import (
    MAX_ROOT,
    MAX_TOTAL,
    build_report,
    clean,
    cut_change,
    old_new,
    readable,
    revision_quote,
)

NS = "shop"


def _comp(name: str, kind: str = "Deployment", **extra) -> dict:
    return {"kind": kind, "name": name, "namespace": NS, "signals": [], **extra}


def _diag(component: str, evidence: list[str] | None = None, category: str = "config_value", sure: float = 0.9, **kw):
    evidence = evidence or []
    return Diagnosis(
        component=component,
        component_result=ChoiceResult(choice=component, confidence=0.9, probabilities={component: 0.9}),
        fault_visible=None,
        category_result=ChoiceResult(choice=category, confidence=sure, probabilities={category: sure}),
        evidence_result=ChoiceResult(choice="E1", confidence=0.9, probabilities={"E1": 0.9}) if evidence else None,
        evidence=[{"id": f"E{i + 1}", "text": t} for i, t in enumerate(evidence)],
        model="m",
        input_tokens=None,
        output_tokens=None,
        investigation={"steps": []},
        **kw,
    )


def _snap(comps: dict, **cluster) -> ClusterSnapshot:
    return ClusterSnapshot(app={"name": "a", "namespaces": [NS]}, components=comps, cluster=cluster)


def _line(text: str, label: str) -> str:
    return next((x for x in text.splitlines() if x.startswith(label + ":")), "")


def test_a_symptom_chosen_as_key_evidence_gives_way_to_the_setting_kubernetes_consults():
    web = _comp(
        "web",
        containers=[{"name": "app", "limits": {"memory": "8Mi"}}],
        pods=[{"name": "web-5d8f9c7b6d-x2k4p", "phase": "Running", "restarts": 3, "template_hash": "5d8f9c7b6d",
               "container_states": ["app: last terminated OOMKilled exit=137"]}],
        template_changes={"changed_at": "2026-01-01T00:00:00Z", "changes": ["container app limits.memory: unset -> 8Mi"]},
    )  # fmt: skip
    d = _diag("deployment/web", ["log x9 (ongoing, last seen 0s ago): app: java.lang.OutOfMemoryError"], "code_bug")
    text = build_report(_snap({"deployment/web": web}), d)
    assert _line(text, "Root cause").endswith("container app limits.memory: unset -> 8Mi.")
    assert "OOMKilled" in _line(text, "Mechanism") and "OutOfMemoryError" not in _line(text, "Root cause")
    assert _line(text, "Fault type").startswith("Fault type: resource limits (")


def test_a_scheduler_message_names_the_host_port_it_could_not_place():
    web = _comp("web", containers=[{"name": "app", "host_ports": [9100]}])
    event = (
        "event FailedScheduling x2 on pod/web-5d8f9c7b6d-x2k4p: 0/3 nodes are available: 1 node(s) had untolerated "
        "taint {node-role.kubernetes.io/control-plane: }, 2 node(s) didn't have free ports for the requested pod ports."
    )
    text = build_report(_snap({"deployment/web": web}), _diag("deployment/web", [event], "scheduling_constraint"))
    assert _line(text, "Root cause").endswith("container app hostPort = 9100.")
    assert "one pod per node" in _line(text, "Mechanism")


def test_a_referenced_object_that_does_not_exist_is_the_subject_with_the_workload_that_references_it():
    web = _comp(
        "web",
        config_refs={"configmaps": ["settings", "present"]},
        config_objects={"configmap/present": {"keys": ["a"]}},
        volumes=[{"name": "conf", "configMap": "settings"}],
    )
    text = build_report(_snap({"deployment/web": web}), _diag("deployment/web", category="missing_object"))
    assert text.startswith(
        "Root cause: ConfigMap `settings` (missing) in namespace `shop`, referenced by Deployment `web`: volume conf "
        "mounts it"
    )
    assert _line(text, "Fault type").startswith("Fault type: missing object")
    # without the decision pointing at it, a missing reference does not take over the subject
    text = build_report(_snap({"deployment/web": web}), _diag("deployment/web", ["container app env MODE=fast"]))
    assert text.startswith("Root cause: Deployment `web`")


def test_a_cronjob_reading_the_written_config_is_the_subject_and_its_target_is_named():
    at = "2026-01-01T00:10:00Z"
    web = _comp(
        "web",
        healthy=False,
        replicas={"desired": 1, "ready": 0},
        template_changes={"changed_at": at, "changes": ["container app limits.memory: unset -> 4Mi"]},
    )
    changes = [
        {"kind": "ConfigMap", "name": "patch-policy", "namespace": "ops", "at": at, "created_after_targets_s": 300,
         "content": '{"data": {"TARGET": "web"}}'},
        {"kind": "CronJob", "name": "patcher", "namespace": "ops", "at": at, "created_after_targets_s": 300,
         "content": '{"spec": {"jobTemplate": {"envFrom": [{"configMapRef": {"name": "patch-policy"}}]}}}'},
    ]  # fmt: skip
    outside = [{"kind": "CronJob", "name": "patcher", "namespace": "ops",
                "signals": ["written by script-client after the application was deployed"]}]  # fmt: skip
    fo = {"kind": "ConfigMap", "name": "patch-policy", "namespace": "ops", "signal": "written by script-client",
          "components": ["deployment/web"]}  # fmt: skip
    snap = _snap(
        {"deployment/web": web}, _changes=changes, outside_objects=outside, symptom_onset="2026-01-01T00:11:00Z"
    )
    text = build_report(snap, _diag(OTHER, fault_object=fo))
    assert text.startswith(
        "Root cause: CronJob `patcher` in namespace `ops`, which reads ConfigMap `patch-policy` and acts on "
        "Deployment `web`"
    )
    assert "on every scheduled run" in _line(text, "Mechanism")
    changed = _line(text, "Changed")
    assert "CronJob patcher created at 2026-01-01T00:10:00Z by script-client, 60s before the first symptom" in changed
    assert "data.TARGET=web" in changed and "limits.memory: unset -> 4Mi" in changed


def test_rbac_relationship_is_resolved_across_findings_not_nearby_changes():
    for name in ("reconciler", "background-worker"):
        cid = f"deployment/{name}"
        role = {"kind": "ClusterRole", "name": "permissions", "signal": "written by operator"}
        rbac = {**role, "components": [cid], "rules": ["configmaps: get,list,watch"]}
        unrelated = _comp(
            "unrelated",
            template_changes={"changed_at": "2026-01-01T00:00:05Z", "changes": ["container app image: old -> new"]},
        )
        snap = _snap(
            {cid: _comp(name), "deployment/unrelated": unrelated},
            findings=[role, rbac],
            rbac=[rbac],
            _changes=[{"kind": "ClusterRole", "name": "permissions", "at": "2026-01-01T00:00:00Z"}],
        )
        for findings in ([role, rbac], [rbac, role]):
            snap.cluster["findings"] = findings
            text = build_report(snap, _diag(OTHER, fault_object=role))
            assert f"acting on Deployment `{name}`" in _line(text, "Root cause")
            assert "unrelated" not in text


def test_nearby_change_alone_does_not_establish_an_object_relationship():
    web = _comp(
        "web", template_changes={"changed_at": "2026-01-01T00:00:00Z", "changes": ["container app image: old -> new"]}
    )
    role = {"kind": "ClusterRole", "name": "unbound", "signal": "rules: configmaps get"}
    snap = _snap({"deployment/web": web}, _changes=[{**role, "at": "2026-01-01T00:00:00Z"}])
    assert "acting on" not in _line(build_report(snap, _diag(OTHER, fault_object=role)), "Root cause")


def test_a_foreign_objects_field_never_becomes_a_workload_setting():
    text = 'Namespace shop was modified 20s ago; now spec = {"finalizers": ["kubernetes"]}; written by helm'
    web = _comp(
        "web",
        signals=[text],
        change_evidence=[{"text": text, "source_object": {"kind": "Namespace", "name": NS, "namespace": None}}],
    )
    d = _diag("deployment/web", [text])
    d.evidence[0]["source_object"] = {"kind": "Namespace", "name": NS, "namespace": None}
    report = build_report(_snap({"deployment/web": web}), d)
    assert "finalizers" not in _line(report, "Root cause")


def test_a_field_on_the_selected_object_can_still_be_reported():
    d = _diag("deployment/web", ["workload dns_policy: None"])
    d.evidence[0]["source_object"] = {"kind": "Deployment", "name": "web", "namespace": NS}
    report = build_report(_snap({"deployment/web": _comp("web")}), d)
    assert "dnsPolicy = None" in _line(report, "Root cause")


def test_chain_characterization_preserves_fact_ownership_and_context():
    from types import SimpleNamespace

    from typesafe_sdk import ChoiceAnswer

    from clients.jev_diag.v2.tree import IterativeDiagnoser

    recorded = []

    def answer(state, questions, **kwargs):
        recorded.append(questions)
        answers = {}
        for name, question in questions.items():
            first = next(iter(question.criteria))
            answers[name] = ChoiceAnswer(
                choice=first, confidence=1, probabilities={key: float(key == first) for key in question.criteria}
            )
        return SimpleNamespace(model="fake", usage=None, choices=answers, nouls={}, answers=answers)

    owner = {"kind": "Namespace", "name": NS, "namespace": None}
    victim = {
        "verdict_probabilities": {"victim": 1},
        "evidence": [{"id": "old", "text": "log x2 (ongoing): authentication rejected"}],
    }
    backend = {
        "evidence": [
            {
                "id": "old",
                "text": "Namespace shop was modified; spec = finalizers=kubernetes",
                "source_object": owner,
                "context": True,
            }
        ]
    }
    snap = _snap({"deployment/client": _comp("client"), "deployment/backend": _comp("backend")})
    diagnoser = IterativeDiagnoser(SimpleNamespace(model="fake", system_one=answer), fetch_detail=lambda *args: {})
    _, key, evidence = diagnoser.chain_characterization(
        snap, "deployment/client", "deployment/backend", {"deployment/client": victim, "deployment/backend": backend}
    )
    assert evidence[1]["source_object"] == owner and evidence[1]["context"] is True
    assert evidence[0]["source_object"] == {"kind": "Deployment", "name": "client", "namespace": NS}
    assert evidence[1]["id"] not in recorded[0]["key_evidence"].criteria
    assert key.choice == evidence[0]["id"]


OTHER = "other"


def test_a_blocked_consumer_is_named_with_the_input_its_settings_read_not_the_one_it_writes():
    reader = _comp("reader")
    d = _diag(
        "deployment/reader",
        ["container reader env OUTPUT_TOPIC=results", "container reader env INPUT_TOPIC=tasks"],
        "data_or_traffic",
        blocked_input={"source": "logs", "evidence": "2026-01-01 00:00:01,100 ERROR item stuck at offset=7"},
    )
    text = build_report(_snap({"deployment/reader": reader}), d)
    assert text.startswith(
        "Root cause: Deployment `reader` in namespace `shop` and its input `tasks`: input INPUT_TOPIC=tasks does not "
        "advance past offset=7."
    )
    assert "fails processing again and again" in _line(text, "Mechanism") and "2026-01-01" not in text


def test_no_raw_document_encoded_data_script_or_timing_is_stated():
    script = "ConfigMap jobs key run.py: " + " ⏎ ".join(f"line_{i} = {i}" for i in range(40))
    encoded = "container loader command: python3 -c import base64; exec(base64.b64decode('" + "QUJD" * 40 + "'))"
    web = _comp(
        "web",
        healthy=False,
        signals=[
            "ConfigMap web-config was created 80s ago, 5s after the workloads that read it; it holds "
            '{"data": {"MODE": "fast"}}; 30s before the first active symptom',
            "warning event BackOff on pod/web-5d8f9c7b6d-x2k4p repeated 3 more time(s) in the last 0s",
        ],
    )
    d = _diag("deployment/web", [script, encoded, "container app env MODE=fast"])
    d.evidence_result = ChoiceResult(choice="E1", confidence=0.4, probabilities={"E1": 0.4, "E2": 0.35, "E3": 0.3})
    text = build_report(_snap({"deployment/web": web}), d)
    assert _line(text, "Root cause").endswith("container app env MODE=fast.")
    for noise in ('{"', "⏎", "base64", "first active symptom", "s ago", "in the last 0s"):
        assert noise not in text, noise


def test_impact_lists_the_failing_components_the_cause_explains_and_never_a_quiet_caller():
    db = _comp(
        "db",
        healthy=False,
        replicas={"desired": 1, "ready": 0},
        pods=[{"name": "db-7f9c8d6b5a-q1w2e", "phase": "Pending", "restarts": 0}],
    )
    line = "app: error: no reachable servers"
    app = _comp(
        "app",
        healthy=False,
        _calls=["deployment/db"],
        log_findings=[{"classes": ["connectivity or timeout"], "count": 5, "targets": [], "line": line}],
    )
    quiet = _comp("quiet", _calls=["deployment/db"])
    snap = _snap({"deployment/db": db, "deployment/app": app, "deployment/quiet": quiet})
    impact = _line(build_report(snap, _diag("deployment/db", ["container db image = store:2"])), "Impact")
    assert impact == (
        "Impact: Deployment `db`: 0/1 replicas ready, Pending; Deployment `app`: connectivity or timeout errors "
        "(5 lines)."
    )


def test_the_fault_type_is_always_stated_from_the_faulty_field_with_the_category_as_fallback():
    web = _comp("web")
    probe = "container app liveness probe: httpGet /live:8080 (periodSeconds=1, failureThreshold=1)"
    text = build_report(_snap({"deployment/web": web}), _diag("deployment/web", [probe], "config_value"))
    assert "Fault type: health probe config (" in text
    assert "periodSeconds=1 (default 10)" in text and "failureThreshold=1 (default 3)" in text
    generic = _diag("deployment/web", ["container app env DB_USER=svc"], "credentials_or_auth", 0.8)
    assert "Fault type: credentials or auth (" in build_report(_snap({"deployment/web": web}), generic)
    unsure = _diag("deployment/web", ["container app env DB_USER=svc"], "credentials_or_auth", 0.3)
    assert "Fault type: config value (" in build_report(_snap({"deployment/web": web}), unsure)
    nothing = _diag("deployment/web", category="other")
    assert "Fault type: not classified." in build_report(_snap({"deployment/web": web}), nothing)


def test_changed_states_when_who_and_old_to_new():
    web = _comp("web", services=[{"name": "web"}])
    changes = [{"kind": "Service", "name": "web", "namespace": NS, "at": "2026-01-01T00:00:00Z",
                "modified_after_creation_s": 900, "values": ['spec.selector: {"app": "web"} -> {"tier": "edge"}']}]  # fmt: skip
    outside = [{"kind": "Service", "name": "web", "namespace": NS,
                "signals": ["written by kubectl-patch after the application was deployed"]}]  # fmt: skip
    fo = {"kind": "Service", "name": "web", "namespace": NS, "signal": "now spec.selector: app=web -> tier=edge",
          "components": ["deployment/web"]}  # fmt: skip
    snap = _snap(
        {"deployment/web": web}, _changes=changes, outside_objects=outside, symptom_onset="2026-01-01T00:01:00Z"
    )
    text = build_report(snap, _diag("deployment/web", fault_object=fo, category="service_routing"))
    assert text.startswith("Root cause: Service `web` in namespace `shop`, selecting Deployment `web`")
    assert _line(text, "Changed") == (
        "Changed: Service web modified at 2026-01-01T00:00:00Z by kubectl-patch, 60s before the first symptom: "
        "spec.selector: app=web -> tier=edge."
    )
    assert "routes only to ready pods whose labels match its selector" in _line(text, "Mechanism")


def test_a_log_quote_comes_only_from_a_workload_running_one_pod_template():
    signals = [
        {"line": "app: WARN retrying soon", "count": 9, "state": "ongoing"},
        {"line": "app: ERROR request failed", "count": 5, "state": "ongoing"},
        {"line": "app: FATAL store schema is newer than this binary", "count": 1, "state": "ongoing"},
        {"line": "app: github.com/x/y.(*Server).Run(0xc000)", "count": 7, "state": "ongoing"},
    ]
    one = {"log_signals": signals, "pods": [{"template_hash": "a"}, {"template_hash": "a"}]}
    assert revision_quote(one) == "FATAL store schema is newer than this binary"
    two = {"log_signals": signals, "pods": [{"template_hash": "a"}, {"template_hash": "b"}]}
    assert revision_quote(two) is None  # an older revision's errors cannot show what the newer one does


def test_every_block_stays_within_its_limits_and_a_cut_change_keeps_its_new_value():
    names = [f"w{i}" for i in range(12)]
    change = "container app image: registry.local/" + "x" * 150 + ":1.0 -> registry.local/app:2.0"
    comps = {
        f"deployment/{n}": _comp(
            n,
            healthy=False,
            replicas={"desired": 3, "ready": 0},
            log_findings=[{"classes": ["connectivity or timeout"], "count": 40, "targets": [], "line": "x"}],
            _calls=["deployment/w0"] if n != "w0" else [],
            template_changes={"changed_at": "2026-01-01T00:00:00Z", "changes": [change]} if n == "w0" else {},
        )
        for n in names
    }
    long = [f"container app env SETTING_{i}={'v' * 60}" for i in range(20)]
    d = _diag("deployment/w0", [change, *long])
    d.evidence_result = ChoiceResult(choice="E1", confidence=0.5, probabilities={f"E{i + 1}": 0.5 for i in range(21)})
    text = build_report(_snap(comps), d)
    assert len(text) <= MAX_TOTAL and len(text.splitlines()[0]) <= MAX_ROOT
    assert "-> registry.local/app:2.0" in text.splitlines()[0]
    assert cut_change("f: " + "o" * 50 + " -> new", 30).endswith("-> new")


def test_documents_read_as_path_value_pairs_and_changes_read_as_old_to_new():
    assert readable('spec.taints = [{"key": "maint", "value": "on", "effect": "NoSchedule"}]') == (
        "spec.taints = maint=on:NoSchedule"
    )
    assert readable("nodeSelector = {'zone': 'b'}") == "nodeSelector = zone=b"
    assert readable('x = {"a": "1", "b": {"c": "2", "d') == "x = a=1, c=2 …"  # a cut document keeps its whole pairs
    assert readable('it holds {"data": {"A": "1"') is None
    assert old_new("container app env A added: 1") == "container app env A: unset -> 1"
    assert old_new("container app env A removed (was 1)") == "container app env A: 1 -> unset"
    assert old_new("container wait added (initContainers, image tool:1)") == "initContainers wait: none -> image tool:1"
    assert clean(
        "spec modified 20s ago, 3s after it was created; pod template changes: container app image: a -> b"
    ) == ("container app image: a -> b")
