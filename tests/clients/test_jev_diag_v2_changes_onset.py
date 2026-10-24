"""Change radar, change ranking and symptom onset: routine controller writes, change sets, active-episode onset, and
failed reads. Neutral synthetic fixtures; each test states a rule, not a recorded problem."""

import pytest

from clients.jev_diag.timeutil import parse_time, set_now
from clients.jev_diag.v2 import changes, derive, outside

NS = "shop"
DEPLOYED = "2026-10-19T08:00:00Z"
NOW = "2026-10-19T08:10:00Z"


@pytest.fixture(autouse=True)
def _clock():
    set_now(parse_time(NOW))
    yield
    set_now(None)


def _comp(name: str, **extra) -> dict:
    return {"kind": "Deployment", "name": name, "namespace": NS, "created": DEPLOYED, "signals": [], **extra}


def _obj(kind: str, name: str, ns: str | None, *writes, created: str = "2026-10-19T08:05:00Z", **body) -> dict:
    """An object with one managedFields entry per (manager, time) write."""
    fields = [{"manager": m, "operation": "Update", "time": t, "fieldsV1": {"f:spec": {}}} for m, t in writes]
    meta = {"name": name, "creationTimestamp": created, "managedFields": fields, **body.pop("meta", {})}
    if ns:
        meta["namespace"] = ns
    return {"kind": kind, "metadata": meta, **body}


def _radar(monkeypatch, items: dict[str, list[dict]], components: dict[str, dict]):
    monkeypatch.setattr(outside, "run_kubectl", lambda args, **k: " ".join(items))
    monkeypatch.setattr(outside, "kubectl_items", lambda resource, ns, errors, **k: items.get(resource, []))
    recorded: list[dict] = []
    found = outside.change_radar([NS], components, [], recorded, parse_time(DEPLOYED))
    return found, {c["name"]: c for c in recorded}


# --------------------------------------------------------------------------- routine controller writes


def test_a_volume_its_provisioner_created_for_a_claim_is_routine_and_a_later_edit_is_a_change(monkeypatch):
    def volume(name: str, *writes) -> dict:
        return _obj(
            "PersistentVolume",
            name,
            None,
            *writes,
            meta={"annotations": {"pv.kubernetes.io/provisioned-by": "example.com/disk"}},
            spec={"claimRef": {"namespace": NS, "name": f"{name}-claim"}},
        )

    items = {
        "persistentvolumes": [
            volume("vol-a", ("disk-provisioner", "2026-10-19T08:05:00Z")),
            volume("vol-b", ("disk-provisioner", "2026-10-19T08:05:00Z"), ("kubectl-patch", "2026-10-19T08:07:00Z")),
        ]
    }
    comps = {"deployment/db": _comp("db", volumes=[{"name": "data", "pvc": "vol-b-claim"}])}
    found, recorded = _radar(monkeypatch, items, comps)
    assert recorded["vol-a"]["routine"] == "volume provisioned for a claim"
    assert not recorded["vol-b"].get("routine") and recorded["vol-b"]["writer"] == "kubectl-patch"
    # the edited volume reaches the workload that mounts its claim; the routine one is no finding of its own
    assert [(f["name"], f["components"]) for f in found] == [("vol-b", ["deployment/db"])]


def test_an_object_its_owners_controller_alone_writes_is_routine(monkeypatch):
    owner = {"meta": {"ownerReferences": [{"kind": "Widget", "name": "w", "controller": True}]}}
    items = {
        "configmaps": [
            _obj("ConfigMap", "generated", "other", ("widget-operator", "2026-10-19T08:05:00Z"), **owner),
            _obj(
                "ConfigMap",
                "edited",
                "other",
                ("widget-operator", "2026-10-19T08:05:00Z"),
                ("kubectl-edit", "2026-10-19T08:06:00Z"),
                **owner,
            ),
        ]
    }
    found, recorded = _radar(monkeypatch, items, {"deployment/app": _comp("app")})
    assert recorded["generated"]["routine"] == "kept by its owner's controller"
    assert not recorded["edited"].get("routine") and [f["name"] for f in found] == ["edited"]


def test_bulk_writes_are_routine_only_from_a_background_writer_that_is_not_a_person(monkeypatch):
    before, after = "2026-10-19T07:00:00Z", "2026-10-19T08:05:00Z"
    items = {
        # a node agent has kept address records since before the application, and keeps writing them
        "addressrecords": [_obj("AddressRecord", "old", None, ("node-agent", before), created=before)]
        + [_obj("AddressRecord", f"rec-{i}", None, ("node-agent", after)) for i in range(3)],
        # a script writes several objects of a kind it never wrote before
        "gadgets": [_obj("Gadget", f"g-{i}", "other", ("script-client", after)) for i in range(3)],
        # the CLI wrote ConfigMaps before the application and three after: intent, never bookkeeping
        "configmaps": [_obj("ConfigMap", "setup", "other", ("kubectl-client-side-apply", before), created=before)]
        + [_obj("ConfigMap", f"cm-{i}", "other", ("kubectl-client-side-apply", after)) for i in range(3)],
        # one write of a kind its writer had written before is a change, not bulk
        "settings": [
            _obj("Setting", "s-old", "other", ("setup-client", before), created=before),
            _obj("Setting", "s-new", "other", ("setup-client", after)),
        ],
    }
    found, recorded = _radar(monkeypatch, items, {"deployment/app": _comp("app")})
    assert {n for n, c in recorded.items() if c.get("routine")} == {"rec-0", "rec-1", "rec-2"}
    assert {f["kind"] for f in found} == {"Gadget", "ConfigMap", "Setting"}


def test_an_operator_write_to_telemetry_is_marked_but_not_hidden(monkeypatch):
    items = {"services": [_obj("Service", "zipkin-collector", NS, ("kubectl-create", "2026-10-19T08:05:00Z"))]}
    found, recorded = _radar(monkeypatch, items, {"deployment/app": _comp("app")})
    assert [f["name"] for f in found] == ["zipkin-collector"]
    assert recorded["zipkin-collector"]["role"] == "observability"


def _change(name: str, at: str, affects=(), **extra) -> dict:
    return {
        "object": f"configmap/{NS}/{name}",
        "kind": "ConfigMap",
        "name": name,
        "namespace": NS,
        "created": DEPLOYED,
        "at": at,
        "affects": list(affects),
        "modified_after_creation_s": 60,
        **extra,
    }


def test_routine_changes_rank_last_are_summarized_once_and_never_displace_a_linked_change():
    comps = {
        "deployment/sick": _comp("sick", signals=["only 0 of 1 desired replicas are ready"]),
        "deployment/well": _comp("well"),
    }
    routine = [
        _change(f"rec-{i}", "2026-10-19T08:06:59Z", writer="node-agent", routine="bulk bookkeeping") for i in range(12)
    ]
    restart = {
        **_change("sick", "2026-10-19T08:06:58Z", affects=["deployment/sick"], restart_only=True),
        "object": "deployment/sick",
        "kind": "Deployment",
    }
    linked = _change("config", "2026-10-19T08:02:00Z", affects=["deployment/sick"], writer="kubectl-edit")
    cluster_wide = _change("cluster-setting", "2026-10-19T08:06:00Z", writer="kubectl-edit")  # acts on no component
    cluster = {"_changes": [*routine, restart, cluster_wide, linked]}
    ranked = changes.rank_changes(comps, cluster, parse_time(NOW))
    assert [c["object"].split("/")[-1] for c in cluster["recent_changes"]] == ["config", "cluster-setting"]
    assert [c["name"] for c in ranked[2:]] == [c["name"] for c in ranked if c["routine"]]
    signals = comps["deployment/sick"]["signals"]
    assert any(s.startswith("ConfigMap config was modified") for s in signals)
    assert not any(s.startswith("spec modified") for s in signals)  # the restart, routine, is no signal
    findings = derive.cluster_findings(cluster, ranked, comps)
    summaries = [f for f in findings if f.get("routine")]
    assert findings[-len(summaries) :] == summaries and len(summaries) == 1  # the restart is the workload's own
    assert summaries[0]["signal"].startswith("routine, bulk bookkeeping: 12 ConfigMap object(s) written by node-agent")
    assert "and 9 more" in summaries[0]["signal"]


def test_a_change_that_acts_on_an_abnormal_component_ranks_before_a_nearer_unlinked_one():
    comps = {"deployment/sick": _comp("sick", log_error_lines=40), "deployment/well": _comp("well")}
    cluster = {
        "_changes": [
            _change("near", "2026-10-19T08:09:00Z", affects=["deployment/well"]),
            _change("unreferenced", "2026-10-19T08:08:00Z"),
            _change("far", "2026-10-19T08:01:00Z", affects=["deployment/sick"]),
        ]
    }
    changes.rank_changes(comps, cluster, parse_time(NOW))
    assert [c["object"].split("/")[-1] for c in cluster["recent_changes"]] == ["far", "near", "unreferenced"]


# --------------------------------------------------------------------------- writers and change sets


def test_a_change_names_its_writer():
    meta = {
        "creationTimestamp": DEPLOYED,
        "managedFields": [
            {"manager": "deployer", "operation": "Update", "time": DEPLOYED, "fieldsV1": {"f:spec": {}}},
            {"manager": "kubectl-edit", "operation": "Update", "time": "2026-10-19T08:05:00Z", "fieldsV1": {}},
            {"manager": "kube-controller-manager", "operation": "Update", "time": "2026-10-19T08:05:01Z"},
        ],
    }
    edited = changes.object_change("deployment/app", "Deployment", "app", NS, meta)
    assert edited["writer"] == "kubectl-edit"  # Kubernetes' own components reconcile; they are not the writer
    late_meta = {"creationTimestamp": "2026-10-19T08:05:00Z", "managedFields": [meta["managedFields"][1]]}
    created = changes.object_change("configmap/x", "ConfigMap", "x", NS, late_meta, targets_created=[DEPLOYED])
    assert created["writer"] == "kubectl-edit"
    revised = changes.object_change(
        "deployment/b", "Deployment", "b", NS, {"creationTimestamp": DEPLOYED}, revised_at="2026-10-19T08:05:00Z"
    )
    assert revised["writer"] is None  # a revision diff alone records no write


def test_writes_by_one_writer_seconds_apart_form_a_change_set():
    records = [
        _change("a", "2026-10-19T08:05:00Z", writer="deployer"),
        _change("b", "2026-10-19T08:05:04Z", writer="deployer"),
        _change("c", "2026-10-19T08:05:02Z", writer="someone-else"),
        _change("d", "2026-10-19T08:10:00Z", writer="deployer"),
        _change("e", "2026-10-19T08:05:01Z", writer="deployer", routine="bulk bookkeeping"),
    ]
    changes.assign_change_sets(records)
    a, b, c, d, e = records
    assert a["change_set"] == b["change_set"] == "cs1" and a["set_size"] == 2
    assert a["set_members"] == [b["object"]] and b["set_members"] == [a["object"]]
    assert not any("change_set" in r for r in (c, d, e))  # another writer, minutes later, routine


# --------------------------------------------------------------------------- symptom onset from the active episode


def _pod(name: str, **state) -> dict:
    return {"name": name, "phase": "Running", "ready": "1/1", **state}


def test_onset_ignores_a_component_that_recovered_from_warm_up_restarts():
    comps = {
        "deployment/warm": _comp(
            "warm",
            pods=[_pod("warm-1", ready_since="2026-10-19T08:04:00Z", last_crash_at="2026-10-19T08:03:30Z")],
            warning_events=[
                {
                    "reason": "BackOff",
                    "object": "pod/warm-1",
                    "first_seen": "2026-10-19T08:01:00Z",
                    "last_seen": "2026-10-19T08:03:40Z",
                }
            ],
        ),
        "deployment/failing": _comp(
            "failing", pods=[_pod("failing-1", ready="0/1", not_ready_since="2026-10-19T08:07:00Z")]
        ),
    }
    moment, source = changes.estimate_symptom_onset(comps, {}, parse_time(NOW))
    assert (moment, source) == (parse_time("2026-10-19T08:07:00Z"), "pod not ready in deployment/failing")


def test_a_liveness_killed_pod_ready_between_kills_still_dates_the_onset():
    pod = _pod("probe-1", ready_since="2026-10-19T08:09:40Z", last_crash_at="2026-10-19T08:09:30Z")
    event = {
        "reason": "Unhealthy",
        "object": "pod/probe-1",
        "first_seen": "2026-10-19T08:06:00Z",
        "last_seen": "2026-10-19T08:09:20Z",
    }
    comps = {"deployment/probe": _comp("probe", pods=[pod], warning_events=[event])}
    moment, source = changes.estimate_symptom_onset(comps, {}, parse_time(NOW))
    assert (moment, source) == (parse_time("2026-10-19T08:06:00Z"), "Unhealthy events on deployment/probe")


def test_events_about_a_replaced_pod_date_the_onset_only_while_they_recur():
    def event(last_seen: str) -> dict:
        return {
            "reason": "BackOff",
            "object": "pod/old-1",
            "first_seen": "2026-10-19T08:02:00Z",
            "last_seen": last_seen,
        }

    current = _pod("new-1", ready="0/1", not_ready_since="2026-10-19T08:08:00Z")
    for last_seen, expected in (("2026-10-19T08:03:00Z", "08:08:00"), ("2026-10-19T08:09:50Z", "08:02:00")):
        comps = {"deployment/app": _comp("app", pods=[current], warning_events=[event(last_seen)])}
        moment, _ = changes.estimate_symptom_onset(comps, {}, parse_time(NOW))
        assert moment == parse_time(f"2026-10-19T{expected}Z"), last_seen


def test_telemetry_export_errors_do_not_date_the_onset():
    export = {"line": "app: span exporter failed: deadline exceeded", "state": "ongoing", "first_seen_seconds_ago": 500}
    own = {"line": "app: request handler failed: connection refused", "state": "ongoing", "first_seen_seconds_ago": 90}
    comps = {"deployment/app": _comp("app", log_signals=[export, own])}
    derive.classify_logs(comps)
    moment, source = changes.estimate_symptom_onset(comps, {}, parse_time(NOW))
    assert (moment, source) == (parse_time("2026-10-19T08:08:30Z"), "errors in deployment/app")


def test_a_change_within_the_onset_precision_reads_as_at_the_onset():
    assert changes.relative_to_symptoms(-4) == "; at the first active symptom"
    assert changes.relative_to_symptoms(-40) == "; 40s after the first active symptom"


# --------------------------------------------------------------------------- failed reads are not absence


def test_failed_reads_come_from_errors_and_timeouts_but_not_named_gets():
    errors = [
        "kubectl get configmaps -o json --show-managed-fields -n shop failed: Error from server (InternalError)",
        "Command '['kubectl', 'get', 'secrets', '-o', 'json', '-n', 'shop']' timed out after 20 seconds",
        "kubectl get ingresses -o json --show-managed-fields -A failed: the server is currently unable",
        "dns configmap: kubectl get configmap dns -n system -o json failed: not found",
    ]
    assert derive.failed_reads(errors) == {("configmaps", NS), ("secrets", NS), ("ingresses", None)}


def test_an_ingress_backend_in_a_namespace_whose_services_could_not_be_read_is_not_called_missing(monkeypatch):
    ingress = {
        "metadata": {"name": "edge", "namespace": NS, "creationTimestamp": DEPLOYED},
        "spec": {"rules": [{"http": {"paths": [{"path": "/", "backend": {"service": {"name": "web", "port": {"number": 80}}}}]}}]},
    }  # fmt: skip
    monkeypatch.setattr(
        outside, "kubectl_items", lambda resource, ns, errors, **k: [ingress] if resource == "ingresses" else []
    )
    for errors, word in (
        ([], "does not exist"),
        ([f"kubectl get services -o json -n {NS} failed: timeout"], "not verified"),
    ):
        (found,) = outside.collect_outside([NS], {"deployment/app": _comp("app")}, {}, list(errors), [])
        assert word in found["signals"][0], errors
