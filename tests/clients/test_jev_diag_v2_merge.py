"""Interactions between namespace identity, provenance, eligibility and probe refresh."""

import pytest

from clients.jev_diag.v2.classifier import ChoiceResult
from clients.jev_diag.v2.collector import ClusterSnapshot
from clients.jev_diag.v2.tree import IterativeDiagnoser, code_only_diagnosis


def component(name, **extra):
    return dict(kind="Deployment", name=name, namespace="shop", signals=[], healthy=True, **extra)


def snapshot(components, **cluster):
    return ClusterSnapshot(app={"namespaces": ["shop", "data"]}, components=components, cluster=cluster)


def test_missing_service_uses_the_probed_namespace_not_a_local_namesake(monkeypatch):
    from clients.jev_diag.v2 import probes

    client, backend = "deployment/shop/api", "deployment/data/cache"
    snap = snapshot(
        {
            client: component(
                "api", pods=[{"name": "api-1", "phase": "Running", "ready": "1/1"}], containers=[{"name": "api"}]
            ),
            backend: {**component("cache", containers=[{"name": "cache", "ports": [8080]}]), "namespace": "data"},
        },
        _service_inventory={"shop": ["cache"], "data": []},
    )
    calls = []

    def run(namespace, pod, container, command):
        calls.append(command[-2])
        return 0, "resolve none\nconnect fail bad address"

    monkeypatch.setattr(probes, "run_in_pod", run)
    result = probes.reachability(snap, client, backend)
    assert calls == ["cache.data.svc"]
    assert result.get("missing_reference") == dict(
        kind="Service", name="cache", namespace="data", client=client, dependency=backend, port=8080
    )
    for inventory in ({"shop": []}, {"shop": [], "data": ["cache"]}):
        snap.cluster["_service_inventory"] = inventory
        assert "missing_reference" not in probes.reachability(snap, client, backend)


@pytest.mark.parametrize("later", ["healthy", "unavailable"])
def test_reexamining_a_backend_retains_a_failed_pair_when_another_pair_is_healthy_or_unavailable(later):
    backend, first, second = "deployment/cache", "deployment/a", "deployment/b"
    snap = snapshot({backend: component("cache", pods=[{"phase": "Running", "ready": "1/1"}])})
    calls = []

    def probe(snap, cid, client):
        calls.append(client)
        if client == second and later == "unavailable":
            raise RuntimeError("tool unavailable")
        return dict(
            command="ping",
            output="NOAUTH" if client == first else "PONG",
            meaning="login rejected" if client == first else "the server answers a ping",
            client=client,
            client_credentials="sets no credential variable and reads no Secret",
        )

    diagnoser = IterativeDiagnoser(fetch_detail=lambda *args: {}, probe=probe)
    investigated = {first: dict(victim_p=1, failing_targets=[backend])}
    detail = {}
    diagnoser._maybe_probe(snap, backend, detail, investigated)
    assert "NOAUTH" in detail["probe_text"]
    investigated[second] = dict(victim_p=1, failing_targets=[backend])
    detail = {}
    diagnoser._maybe_probe(snap, backend, detail, investigated)
    assert calls == [first, second]
    assert "NOAUTH" in detail["probe_text"] and "PONG" not in detail["probe_text"]
    diagnoser._maybe_probe(snap, backend, {}, investigated)
    assert calls == [first, second]


def test_namespace_context_does_not_lift_a_cleared_backend():
    context = "Namespace shop was modified"
    comp = component("cache", change_evidence=[{"text": context, "context": True}], most_recent_change=True)
    comp["signals"] = [context]
    backend, client = "deployment/cache", "deployment/api"
    snap = snapshot({backend: comp, client: component("api")})
    triage = ChoiceResult(choice=client, confidence=0.9, probabilities={client: 0.9, backend: 0.1})
    examined = {
        backend: dict(verdict="unrelated", origin_p=0.1, next="none"),
        client: dict(verdict="victim", origin_p=0.1, victim_p=0.9, next=backend, next_p=1, failing_targets=[backend]),
    }
    diagnoser = IterativeDiagnoser(fetch_detail=lambda *args: {})
    assert diagnoser.support(snap, triage, examined)[backend] == (0.1, None)
    comp["signals"].append("only 0 of 1 desired replicas are ready")
    assert diagnoser.support(snap, triage, examined)[backend][0] == 0.9


def test_code_fallback_orders_unknown_health_between_observed_failure_and_health():
    comps = {f"deployment/{name}": component(name) for name in ("known", "unknown", "failed")}
    comps["deployment/unknown"].update(healthy=None, pods_not_verified="Pod list failed")
    comps["deployment/failed"]["healthy"] = False
    assert code_only_diagnosis(snapshot(comps), "offline").component == "deployment/failed"
    del comps["deployment/failed"]
    assert code_only_diagnosis(snapshot(comps), "offline").component == "deployment/unknown"


def test_export_noise_cannot_outvote_an_origin_but_telemetry_own_failure_can():
    real, telemetry = "deployment/api", "deployment/collector"
    comp = component(
        "collector",
        role="observability",
        replicas={"desired": 1, "ready": 1},
        log_findings=[{"classes": ["connectivity or timeout"], "count": 500}],
    )
    snap = snapshot({real: component("api"), telemetry: comp})
    triage = ChoiceResult(choice=real, confidence=0.6, probabilities={real: 0.6, telemetry: 0.4})
    investigated = {
        real: dict(origin_p=0.6, verdict="origin", next="none"),
        telemetry: dict(origin_p=0.99, verdict="origin", next="none"),
    }
    diagnoser = IterativeDiagnoser(fetch_detail=lambda *args: {})
    assert diagnoser.decide(snap, triage, investigated, None, 0)[0] == real
    comp["replicas"]["ready"] = 0
    assert diagnoser.decide(snap, triage, investigated, None, 0)[0] == telemetry


def test_a_shared_write_does_not_bypass_telemetry_eligibility():
    from clients.jev_diag.v2.tree import written_together

    real, telemetry = "deployment/api", "deployment/collector"
    comp = component("collector", role="observability", replicas={"desired": 1, "ready": 1})
    snap = snapshot(
        {real: component("api"), telemetry: comp},
        recent_changes=[
            {"object": real, "change_set": "operation"},
            {"object": telemetry, "change_set": "operation"},
        ],
    )
    assert written_together(snap, [real]) == []
    comp["replicas"]["ready"] = 0
    assert written_together(snap, [real]) == [telemetry]


def reference(client, dependency, name="cache"):
    return dict(kind="Service", name=name, namespace="shop", client=client, dependency=dependency, port=8080)


def test_a_failed_reference_is_kept_when_only_its_client_is_in_the_comparison(monkeypatch):
    gateway, client, backend = "deployment/gateway", "deployment/api", "deployment/cache"
    snap = snapshot({c: component(c.split("/")[-1]) for c in (gateway, client, backend)})
    triage = ChoiceResult(choice=gateway, confidence=0.7, probabilities={gateway: 0.7, client: 0.2, backend: 0.1})
    examined = {
        gateway: dict(origin_p=0.23, verdict="victim", victim_p=0.65, next="none"),
        client: dict(
            origin_p=0.14, verdict="victim", victim_p=0.7, next="none", missing_references=[reference(client, backend)]
        ),
        backend: dict(origin_p=0.01, verdict="unrelated", next="none"),
    }
    diagnoser = IterativeDiagnoser(fetch_detail=lambda *args: {})
    monkeypatch.setattr(diagnoser, "compare_origins", lambda *args, **kwargs: None)
    assert diagnoser.decide(snap, triage, examined, None, 0) == (backend, "verified failed Service reference")
    # Two distinct absent references cannot be collapsed into one definite object.
    examined[client]["missing_references"].append(reference(client, backend, "another-cache"))
    assert diagnoser.decide(snap, triage, examined, None, 0)[1].startswith("unresolved comparison:")


@pytest.mark.parametrize("category", ["missing_object", "dns_resolution"])
def test_a_client_conclusion_reports_its_verified_missing_service(category):
    from clients.jev_diag.v2.report import build_report

    client, backend = "deployment/api", "deployment/cache"
    snap = snapshot({client: component("api"), backend: component("cache")})
    examined = {
        client: dict(
            category=ChoiceResult(choice=category, confidence=1, probabilities={category: 1}),
            key_evidence=None,
            evidence=[],
            origin_p=0.9,
            missing_references=[reference(client, backend)],
        )
    }
    diagnoser = IterativeDiagnoser(fetch_detail=lambda *args: {})
    diagnoser.requests = [{"model": "fake", "usage": {}}]
    info = diagnoser._characterize(snap, client, examined, None, None, None)
    choice = ChoiceResult(choice=client, confidence=0.9, probabilities={client: 0.9})
    diagnosis = diagnoser._diagnosis(snap, client, choice, info, None)
    text = build_report(snap, diagnosis)
    assert text.startswith("Root cause: Service `cache` (missing) in namespace `shop`")
    assert "cannot resolve cache:8080" in text and "inventory confirms it is absent" in text
    # The observation cannot rewrite a different selected causal category.
    examined[client]["category"] = ChoiceResult(choice="config_value", confidence=1, probabilities={"config_value": 1})
    assert not diagnoser._characterize(snap, client, examined, None, None, None).get("failed_reference")


def test_a_probability_gap_cannot_promote_a_victim_over_a_verified_failed_reference():
    gateway, client, backend = "deployment/gateway", "deployment/api", "deployment/cache"
    snap = snapshot({c: component(c.split("/")[-1]) for c in (gateway, client, backend)})
    triage = ChoiceResult(choice=gateway, confidence=0.7, probabilities={gateway: 0.7, client: 0.2, backend: 0.1})
    examined = {
        gateway: dict(origin_p=0.4, verdict="victim", victim_p=0.6, next="none"),
        client: dict(
            origin_p=0.2, verdict="victim", victim_p=0.8, next="none", missing_references=[reference(client, backend)]
        ),
        backend: dict(origin_p=0.01, verdict="unrelated", next="none"),
    }
    diagnoser = IterativeDiagnoser(fetch_detail=lambda *args: {})
    assert diagnoser.decide(snap, triage, examined, None, 0) == (backend, "verified failed Service reference")
    # An independent origin is not erased by a different client's failed reference.
    examined[gateway].update(origin_p=0.9, verdict="origin", victim_p=0.1)
    assert diagnoser.decide(snap, triage, examined, None, 0)[0] == gateway
