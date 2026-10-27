"""jev_diag v1.1 decision step (tree.py): what is concluded from the examinations, and which questions are asked."""

from __future__ import annotations

from types import SimpleNamespace

from clients.jev_diag.v1_1.collector import ClusterSnapshot

NS = "shop"


def _comp(name: str, **extra) -> dict:
    return {"kind": "Deployment", "name": name, "namespace": NS, "signals": ["s"], "healthy": False, **extra}


def _snap(comps: dict) -> ClusterSnapshot:
    return ClusterSnapshot(app={"name": "a", "namespaces": [NS]}, components=comps, cluster={})


class Scripted:
    """A fake decision model: triage from a script, each examined candidate's verdict from a table, every other
    choice from `picks` (an option, or {option: probability}) or its first option. Records every request label."""

    def __init__(self, triage: dict[str, float], verdicts: dict[str, dict[str, float]], picks: dict | None = None):
        self.triage, self.verdicts, self.picks, self.model, self.labels = triage, verdicts, picks or {}, "fake", []

    def system_one(self, state, questions, **kwargs):
        from typesafe_sdk import Choice, ChoiceAnswer, NoulAnswer

        from clients.jev_diag.v1_1.classifier import COMPONENT_QUESTION

        answers = {}
        for name, q in questions.items():
            if name == COMPONENT_QUESTION:
                probs = dict(self.triage)
            elif name == "verdict":
                probs = self.verdicts.get(state["candidate"]["id"], {"unrelated": 1.0})
                probs = {o: probs.get(o, 0.0) for o in ("origin", "victim", "unrelated", "undetermined")}
            elif isinstance(q, Choice):
                opts = list(q.criteria)
                pick = self.picks.get(name, opts[0])
                probs = {o: pick.get(o, 0.0) if isinstance(pick, dict) else float(o == pick) for o in opts}
            else:
                answers[name] = NoulAnswer(noul=0.1)
                continue
            best = max(probs, key=probs.get)
            answers[name] = ChoiceAnswer(choice=best, confidence=probs[best], probabilities=probs)
        choices = {k: v for k, v in answers.items() if isinstance(v, ChoiceAnswer)}
        nouls = {k: v for k, v in answers.items() if isinstance(v, NoulAnswer)}
        return SimpleNamespace(model="fake", usage=None, choices=choices, nouls=nouls, answers=dict(answers))


def _run(comps, triage, verdicts, picks=None, detail=None):
    from clients.jev_diag.v1_1.tree import IterativeDiagnoser

    model = Scripted({**triage, "other": 0.0}, verdicts, picks)
    diagnoser = IterativeDiagnoser(model, fetch_detail=lambda s, c: dict((detail or {}).get(c) or {}))
    diagnoser.requests = []
    original = diagnoser._ask

    def ask(label, state, questions):
        model.labels.append(label)
        return original(label, state, questions)

    diagnoser._ask = ask
    return diagnoser.diagnose(_snap(comps)), model.labels


def test_triage_asks_for_the_component_and_the_fault_object_kind_only():
    from clients.jev_diag.v1_1.classifier import build_component_questions

    questions = build_component_questions(_snap({"deployment/a": _comp("a")}))
    assert set(questions) == {"root_cause_component", "fault_object_kind"}


def test_configured_model_limit_reaches_the_production_diagnoser(monkeypatch):
    import sys

    from typesafe_sdk import Choice, ChoiceAnswer, NoulAnswer

    from clients.jev_diag import driver
    from clients.jev_diag.v1_1.driver import make_diagnoser

    monkeypatch.setenv("JEV_DIAG_MAX_CHOICE_OPTIONS", "26")
    monkeypatch.setenv("JEV_DIAG_V1_1_PROBES", "0")
    monkeypatch.setattr(sys, "argv", ["jev_diag", "--mode", "v1.1"])
    args = driver.parse_args()

    class Limited:
        counts = []

        def system_one(self, state, questions):
            answers = {}
            for name, question in questions.items():
                if isinstance(question, Choice):
                    options = list(question.criteria)
                    self.counts.append(len(options))
                    assert 2 <= len(options) <= 26
                    answers[name] = ChoiceAnswer(
                        choice=options[0], confidence=1.0, probabilities={o: float(o == options[0]) for o in options}
                    )
                else:
                    answers[name] = NoulAnswer(noul=0.0)
            return SimpleNamespace(
                model="limited",
                usage=None,
                answers=answers,
                choices={k: v for k, v in answers.items() if isinstance(v, ChoiceAnswer)},
                nouls={k: v for k, v in answers.items() if isinstance(v, NoulAnswer)},
            )

    model = Limited()
    diagnoser = make_diagnoser(args, None, client=model)
    diagnoser.fetch_detail = lambda *args: {}
    comps = {f"deployment/svc-{i}": _comp(f"svc-{i}", signals=[], healthy=True) for i in range(30)}
    comps["deployment/svc-0"].update(signals=["no pods exist"], healthy=False)
    diagnosis = diagnoser.diagnose(_snap(comps))
    assert diagnoser.max_choice_options == 26
    assert max(model.counts) == 26
    assert "decision model" not in (diagnosis.investigation["fallback"] or "")


def test_a_clear_leader_is_concluded_without_a_comparison():
    comps = {"deployment/a": _comp("a"), "deployment/b": _comp("b")}
    verdicts = {"deployment/a": {"origin": 0.9, "victim": 0.1}, "deployment/b": {"origin": 0.5, "undetermined": 0.5}}
    d, labels = _run(comps, {"deployment/a": 0.6, "deployment/b": 0.4}, verdicts)
    assert d.component == "deployment/a"
    assert not any(label.startswith("tiebreak") for label in labels)


def test_close_leaders_are_compared_in_both_orders_whatever_the_threshold():
    comps = {"deployment/a": _comp("a"), "deployment/b": _comp("b")}
    # 0.62 and 0.59 sit on either side of the old 0.6 confirmation threshold: the comparison decides, not the bar
    verdicts = {"deployment/a": {"origin": 0.62, "victim": 0.38}, "deployment/b": {"origin": 0.59, "victim": 0.41}}
    d, labels = _run(comps, {"deployment/a": 0.6, "deployment/b": 0.4}, verdicts, {"fault_start": "deployment/b"})
    assert d.component == "deployment/b"
    assert [label for label in labels if label.startswith("tiebreak")] == [
        "tiebreak:deployment/a|deployment/b",
        "tiebreak:deployment/b|deployment/a",
    ]


def test_a_victims_pointer_supports_the_dependency_it_names():
    comps = {"deployment/web": _comp("web"), "deployment/store": _comp("store", signals=[], healthy=True)}
    verdicts = {
        "deployment/web": {"victim": 0.95, "origin": 0.05},
        "deployment/store": {"undetermined": 0.7, "origin": 0.2, "victim": 0.1},
    }
    detail = {"deployment/web": {"failing_targets": ["deployment/store"]}}
    d, _ = _run(
        comps,
        {"deployment/web": 0.9, "deployment/store": 0.1},
        verdicts,
        {"next_component": "deployment/store"},
        detail,
    )
    # the store looks healthy, yet the client's failing calls name it surely: the fault is there, not in the client
    assert d.component == "deployment/store"
    assert d.investigation["fallback"].startswith("end of a victim chain: deployment/web fails calling")


def test_without_any_examined_candidate_triage_decides():
    from clients.jev_diag.v1_1.classifier import ChoiceResult
    from clients.jev_diag.v1_1.tree import IterativeDiagnoser

    snap = _snap({"deployment/a": _comp("a"), "deployment/b": _comp("b")})
    triage = ChoiceResult(
        choice="deployment/b", confidence=0.7, probabilities={"deployment/b": 0.7, "deployment/a": 0.3}
    )
    diagnoser = IterativeDiagnoser(None, fetch_detail=lambda s, c: {})
    assert diagnoser.decide(snap, triage, {}, None, 0.0) == (
        "deployment/b",
        "triage top-1 eligible candidate",
    )


def test_an_examination_sees_the_incident_it_has_to_explain():
    from clients.jev_diag.v1_1.tree import incident

    comps = {
        "deployment/web": _comp(
            "web",
            replicas={"desired": 2, "ready": 0},
            log_findings=[{"count": 7, "classes": ["connectivity or timeout"]}],
        ),
        "deployment/quiet": _comp("quiet", signals=[], healthy=True, replicas={"desired": 1, "ready": 1}),
        # a telemetry component counts by its own state, not by its (export) log lines
        "deployment/collector": _comp(
            "collector",
            role="observability",
            replicas={"desired": 1, "ready": 0},
            log_findings=[{"count": 40, "classes": ["connectivity or timeout"]}],
        ),
        "deployment/exporter": _comp(
            "exporter",
            role="observability",
            replicas={"desired": 1, "ready": 1},
            log_findings=[{"count": 40, "classes": ["connectivity or timeout"]}],
        ),
    }
    assert incident(_snap(comps)) == [
        {"component": "deployment/web", "symptoms": "0/2 replicas ready, connectivity or timeout errors (7 lines)"},
        {"component": "deployment/collector", "symptoms": "0/1 replicas ready"},
    ]


def test_role_does_not_remove_a_nonleading_fault_candidate_or_its_support():
    for role in ("application", "observability"):
        comps = {
            "deployment/web": _comp("web"),
            "deployment/backend": _comp(
                "backend", role=role, replicas={"desired": 2, "ready": 1}, pods=[{"phase": "Pending"}]
            ),
        }
        verdicts = {
            "deployment/web": {"origin": 0.7, "undetermined": 0.3},
            "deployment/backend": {"origin": 0.99, "unrelated": 0.01},
        }
        d, labels = _run(comps, {"deployment/web": 0.65, "deployment/backend": 0.35}, verdicts)
        assert "investigate:deployment/backend" in labels
        assert d.component == "deployment/backend"


def test_export_errors_alone_do_not_define_an_incident_but_pending_telemetry_does():
    from clients.jev_diag.v1_1.tree import incident

    comp = _comp(
        "collector",
        role="observability",
        signals=[],
        replicas={"desired": 1, "ready": 1},
        log_signals=[{"line": "failed to export traces: context deadline exceeded", "state": "ongoing"}],
    )
    assert incident(_snap({"deployment/collector": comp})) == []
    comp["pods"] = [{"phase": "Pending"}]
    assert incident(_snap({"deployment/collector": comp})) == [
        {"component": "deployment/collector", "symptoms": "Pending"}
    ]


def _healthy_backend():
    return _comp("backend", signals=[], healthy=True, pods=[{"phase": "Running", "ready": "1/1"}])


def _probe_result():
    return {"command": "protocol-health", "output": "authentication required", "meaning": "login rejected"}


def test_collected_failed_calls_allow_a_probe_before_a_client_verdict_and_are_cached():
    from clients.jev_diag.v1_1.tree import IterativeDiagnoser

    calls = []
    backend = _healthy_backend()
    parent = "Namespace shop was modified; now spec = finalizers=kubernetes"
    backend.update(signals=[parent], change_evidence=[{"text": parent, "context": True}])
    snap = _snap(
        {"deployment/backend": backend, "deployment/client": _comp("client", errors_point_to=["deployment/backend"])}
    )
    diagnoser = IterativeDiagnoser(
        fetch_detail=lambda *args: {}, probe=lambda s, c, client: calls.append((c, client)) or _probe_result()
    )
    for investigated in ({}, {"deployment/client": {"victim_p": 0.9, "failing_targets": ["deployment/backend"]}}):
        detail = {}
        diagnoser._maybe_probe(snap, "deployment/backend", detail, investigated)
        assert detail["probe"]["meaning"] == "login rejected"
    assert calls == [("deployment/backend", "deployment/client")]


def test_a_new_failure_relationship_reopens_a_backend_within_the_step_budget():
    from clients.jev_diag.v1_1.tree import IterativeDiagnoser

    class Model(Scripted):
        def system_one(self, state, questions, **kwargs):
            candidate = state.get("candidate") or {}
            if candidate.get("id") == "deployment/backend" and candidate.get("detail", {}).get("probe"):
                self.verdicts["deployment/backend"] = {"origin": 0.99, "unrelated": 0.01}
            if candidate:
                self.labels.append(candidate["id"])
            return super().system_one(state, questions, **kwargs)

    for budget in (2, 3):
        calls = []
        model = Model(
            {"deployment/backend": 0.6, "deployment/client": 0.4},
            {"deployment/backend": {"unrelated": 1}, "deployment/client": {"victim": 1}},
            {"next_component": "deployment/backend"},
        )

        def detail(snapshot, cid):
            return {"failing_targets": ["deployment/backend"]} if cid == "deployment/client" else {}

        diagnoser = IterativeDiagnoser(
            model,
            fetch_detail=detail,
            max_steps=budget,
            probe=lambda s, c, client, calls=calls: calls.append((c, client)) or _probe_result(),
        )
        d = diagnoser.diagnose(_snap({"deployment/backend": _healthy_backend(), "deployment/client": _comp("client")}))
        assert len(d.investigation["steps"]) <= budget
        if budget == 3:
            assert model.labels == ["deployment/backend", "deployment/client", "deployment/backend"]
            assert calls == [("deployment/backend", "deployment/client")]
            assert d.component == "deployment/backend"
        else:
            assert model.labels == ["deployment/backend", "deployment/client"]
            assert calls == []


def test_an_unavailable_probe_is_not_retried_for_the_same_pair():
    from clients.jev_diag.v1_1.tree import IterativeDiagnoser

    calls = []
    snap = _snap(
        {
            "deployment/backend": _healthy_backend(),
            "deployment/client": _comp("client", errors_point_to=["deployment/backend"]),
        }
    )

    def unavailable(*args):
        calls.append(args)
        raise RuntimeError("tool unavailable")

    diagnoser = IterativeDiagnoser(fetch_detail=lambda *args: {}, probe=unavailable)
    for _ in range(2):
        detail = {}
        diagnoser._maybe_probe(snap, "deployment/backend", detail, {})
        assert "probe" not in detail
    assert len(calls) == 1


def test_unverified_or_nonrunning_backends_are_not_probed_as_healthy():
    from clients.jev_diag.v1_1.tree import IterativeDiagnoser

    for extra in (
        {"pods_not_verified": True},
        {"pods": []},
        {"pods": [{"phase": "Pending", "ready": "0/1"}]},
        {"pods": [{"phase": "Running", "ready": "0/0"}]},
    ):
        backend = {**_healthy_backend(), **extra}
        calls = []
        snap = _snap(
            {
                "deployment/backend": backend,
                "deployment/client": _comp("client", errors_point_to=["deployment/backend"]),
            }
        )
        diagnoser = IterativeDiagnoser(
            fetch_detail=lambda *args: {}, probe=lambda *args, calls=calls: calls.append(args) or _probe_result()
        )
        detail = {}
        diagnoser._maybe_probe(snap, "deployment/backend", detail, {})
        assert calls == [] and "probe" not in detail


def test_objects_written_in_one_operation_with_a_hypothesis_become_hypotheses():
    from clients.jev_diag.v1_1.tree import OTHER_OPTION, written_together

    snap = _snap({"deployment/a": _comp("a"), "deployment/b": _comp("b"), "deployment/c": _comp("c")})
    snap.cluster["recent_changes"] = [
        {"object": "deployment/a", "change_set": "cs1", "writer": "w"},
        {"object": "cronjob/ops/patcher", "change_set": "cs1", "writer": "w"},
        {"object": "deployment/b", "change_set": "cs1", "writer": "w"},
        {"object": "deployment/c", "writer": "other"},
    ]
    snap.cluster["findings"] = [{"kind": "CronJob", "name": "patcher", "change_set": "cs1", "signal": "created"}]
    assert written_together(snap, ["deployment/a"]) == [OTHER_OPTION, "deployment/b"]
    assert written_together(snap, ["deployment/c"]) == []  # a change of its own, written by no shared operation


def test_a_platform_layer_failing_for_several_workloads_is_a_finding_and_a_hypothesis():
    from clients.jev_diag.v1_1.derive import platform_findings

    comps = {
        "deployment/a": _comp("a", platform_failures={"pod_network": 6}),
        "deployment/b": _comp("b", platform_failures={"pod_network": 2}),
        "deployment/c": _comp("c", platform_failures={"preemption": 1}),
    }
    (finding,) = platform_findings(comps)  # one workload preempted is that workload's own business
    assert finding["kind"] == "Platform" and finding["name"] == "pod network" and finding["platform"]
    assert finding["components"] == ["deployment/a", "deployment/b"]
    assert "the events of 2 workloads state it" in finding["signal"]


class PositionBiased(Scripted):
    """Picks the side listed first in every comparison, more strongly the second time."""

    shares = (0.8, 0.9)

    def system_one(self, state, questions, **kwargs):
        if "fault_start" not in questions:
            return super().system_one(state, questions, **kwargs)
        from typesafe_sdk import ChoiceAnswer

        first, second = list(questions["fault_start"].criteria)
        share = self.shares[sum(label.startswith("tiebreak") for label in self.labels) > 1]
        probs = {first: share, second: 1 - share}
        answer = ChoiceAnswer(choice=first, confidence=share, probabilities=probs)
        return SimpleNamespace(model="fake", usage=None, choices={"fault_start": answer}, nouls={}, answers={})


def test_a_comparison_whose_orders_disagree_keeps_the_better_examined_side():
    from clients.jev_diag.v1_1.tree import IterativeDiagnoser

    comps = {"deployment/a": _comp("a"), "deployment/b": _comp("b")}
    verdicts = {"deployment/a": {"origin": 0.62, "victim": 0.38}, "deployment/b": {"origin": 0.59, "victim": 0.41}}
    model = PositionBiased({"deployment/a": 0.6, "deployment/b": 0.4, "other": 0.0}, verdicts)
    diagnoser = IterativeDiagnoser(model, fetch_detail=lambda s, c: {})
    diagnoser.requests = []
    original = diagnoser._ask

    def ask(label, state, questions):
        model.labels.append(label)
        return original(label, state, questions)

    diagnoser._ask = ask
    # averaged, b would win (0.55 against 0.45) only because each order preferred the side it listed first
    assert diagnoser.diagnose(_snap(comps)).component == "deployment/a"


def test_for_a_permission_fault_the_role_with_the_rules_is_named_before_its_binding():
    from clients.jev_diag.v1_1.classifier import ChoiceResult, select_fault_object

    snap = _snap({"deployment/worker": _comp("worker")})
    snap.cluster["findings"] = [
        {"kind": "ClusterRoleBinding", "name": "worker", "signal": "ClusterRoleBinding worker was created 60s ago",
         "components": ["deployment/worker"]},
        {"kind": "ClusterRole", "name": "worker", "signal": "ClusterRole worker was created 60s ago",
         "components": ["deployment/worker"]},
    ]  # fmt: skip
    kind = ChoiceResult(choice="rbac", confidence=1.0, probabilities={"rbac": 1.0})
    assert select_fault_object(snap, "deployment/worker", kind)["kind"] == "ClusterRole"


def test_a_backend_that_answers_a_client_set_up_the_same_way_is_no_finding():
    from clients.jev_diag.v1_1.probes import agrees_with_client

    none = "sets no credential variable and reads no Secret"
    assert agrees_with_client({"meaning": "the server answers without authentication", "client_credentials": none})
    assert not agrees_with_client(
        {"meaning": "the server answers without authentication", "client_credentials": "sets credential variables PASS"}
    )
    assert not agrees_with_client({"meaning": "the server requires authentication for this command", "client": "c"})
    assert agrees_with_client({"meaning": "the server accepts connections"})


def test_a_client_holding_a_value_its_own_secret_replaced_is_the_origin_not_the_server():
    from clients.jev_diag.v1_1.tree import VERDICTS

    assert "keeps the value it had when the container started" in VERDICTS["origin"]["what"]
    assert "a server that rejects a credential a client still holds" in VERDICTS["origin"]["not_for"]


def test_a_pointer_does_not_lift_a_dependency_judged_unrelated_that_shows_nothing_of_its_own():
    comps = {
        "deployment/web": _comp("web"),
        "deployment/store": _comp("store", signals=[], healthy=True),
        "deployment/tracing": _comp("tracing", replicas={"desired": 2, "ready": 1}),
    }
    verdicts = {
        "deployment/tracing": {"origin": 0.3, "unrelated": 0.7},
        "deployment/web": {"victim": 0.95, "origin": 0.05},
        "deployment/store": {"unrelated": 0.9, "origin": 0.1},
    }
    detail = {"deployment/web": {"failing_targets": ["deployment/store"]}}
    triage = {"deployment/tracing": 0.6, "deployment/web": 0.3, "deployment/store": 0.1}
    d, _ = _run(comps, triage, verdicts, {"next_component": "deployment/store"}, detail)
    # nothing in the store changed or shows a fault, and its own examination cleared it: the pointer does not win
    assert d.component == "deployment/tracing"
    # the same store, rejecting logins, is still found through its client's failing calls
    comps["deployment/store"] = _comp("store", signals=["rejects logins for account app (5 lines)"], healthy=True)
    d, _ = _run(comps, triage, verdicts, {"next_component": "deployment/store"}, detail)
    assert d.component == "deployment/store"


def test_examinations_and_comparisons_carry_the_rules_that_decide_ownership():
    from clients.jev_diag.v1_1.tree import VERDICTS, build_tiebreak_questions

    assert "judged like any other component" in VERDICTS["unrelated"]["not_for"]
    steps = " ".join(
        build_tiebreak_questions(["deployment/a", "deployment/b"])["fault_start"].instructions["how_to_decide"]
    )
    assert (
        "Adoption alone does not establish correctness" in steps
        and "keeps the value it had when the container started" in steps
    )


def _biased_diagnosis(comps, verdicts, detail=None):
    from clients.jev_diag.v1_1.tree import IterativeDiagnoser

    model = PositionBiased({c: p for c, p in zip(comps, (0.7, 0.3), strict=True)}, verdicts)
    diagnoser = IterativeDiagnoser(model, fetch_detail=lambda s, c: dict((detail or {}).get(c) or {}))
    original = diagnoser._ask

    def ask(label, state, questions):
        model.labels.append(label)
        return original(label, state, questions)

    diagnoser._ask = ask
    return diagnoser.diagnose(_snap(comps))


def test_disagreement_between_weak_non_origins_is_reported_as_unresolved():
    comps = {"deployment/gateway": _comp("gateway"), "deployment/catalog": _comp("catalog")}
    verdicts = {
        "deployment/gateway": {"origin": 0.23, "victim": 0.65, "undetermined": 0.12},
        "deployment/catalog": {"origin": 0.14, "unrelated": 0.76, "undetermined": 0.10},
    }
    diagnosis = _biased_diagnosis(comps, verdicts)
    assert diagnosis.text.startswith("Root cause: unresolved.")
    assert "Fault type:" not in diagnosis.text and "Mechanism:" not in diagnosis.text


def test_an_unresolved_comparison_keeps_a_verified_failed_service_reference():
    comps = {"deployment/gateway": _comp("gateway"), "deployment/catalog": _comp("catalog")}
    verdicts = {
        "deployment/gateway": {"origin": 0.23, "victim": 0.65, "undetermined": 0.12},
        "deployment/catalog": {"origin": 0.14, "unrelated": 0.76, "undetermined": 0.10},
    }
    reference = {
        "kind": "Service",
        "name": "catalog",
        "namespace": NS,
        "client": "deployment/gateway",
        "dependency": "deployment/catalog",
        "port": 8080,
    }
    diagnosis = _biased_diagnosis(comps, verdicts, {"deployment/gateway": {"missing_references": [reference]}})
    assert diagnosis.component == "deployment/catalog"
    assert diagnosis.text.startswith("Root cause: Service `catalog` (missing)")
    assert "cannot resolve catalog:8080" in diagnosis.text
    assert "Fault type: missing object" in diagnosis.text


def test_a_telemetry_own_failure_is_examined_and_can_win_below_the_triage_cutoff():
    comps = {
        "deployment/web": _comp("web"),
        "deployment/traces": _comp("traces", role="observability", signals=[], replicas={"desired": 2, "ready": 1}),
    }
    verdicts = {"deployment/web": {"origin": 0.7, "victim": 0.3}, "deployment/traces": {"origin": 0.99, "victim": 0.01}}
    diagnosis, labels = _run(comps, {"deployment/web": 0.96, "deployment/traces": 0.04}, verdicts)
    assert "investigate:deployment/traces" in labels
    assert diagnosis.component == "deployment/traces"


def test_telemetry_own_failure_is_eligible_for_links_support_and_fallback():
    from clients.jev_diag.v1_1.classifier import ChoiceResult
    from clients.jev_diag.v1_1.tree import IterativeDiagnoser, linked_components

    snap = _snap(
        {
            "deployment/web": _comp("web"),
            "deployment/traces": _comp("traces", role="observability", signals=[], replicas={"desired": 2, "ready": 1}),
        }
    )
    assert linked_components(snap, "deployment/web", {"calls": {"deployment/traces": {}}}) == ["deployment/traces"]
    triage = ChoiceResult(
        choice="deployment/web", confidence=0.8, probabilities={"deployment/web": 0.8, "deployment/traces": 0.2}
    )
    diagnoser = IterativeDiagnoser(None, fetch_detail=lambda s, c: {})
    result = {"verdict": "origin", "origin_p": 0.9, "next": "none"}
    assert diagnoser.support(snap, triage, {"deployment/traces": result}) == {"deployment/traces": (0.9, None)}
    only = ChoiceResult(choice="deployment/traces", confidence=0.9, probabilities={"deployment/traces": 0.9})
    assert diagnoser.fallback(snap, only)[0] == "deployment/traces"
    snap.components["deployment/traces"]["replicas"]["ready"] = 2
    assert linked_components(snap, "deployment/web", {"calls": {"deployment/traces": {}}}) == []
    assert "deployment/traces" not in diagnoser.support(snap, triage, {"deployment/traces": result})


def test_comparison_carries_adoption_facts_without_clearing_the_updated_side():
    from clients.jev_diag.v1_1.classifier import ChoiceResult
    from clients.jev_diag.v1_1.tree import IterativeDiagnoser, build_tiebreak_questions

    comp = _comp("reader", _holds_previous={"secret/connection": "2026-01-01T12:00:00Z"})
    result = {
        "verdict": "origin",
        "category": ChoiceResult("credentials_or_auth", 0.9, {"credentials_or_auth": 0.9}),
        "key_evidence": None,
        "spec_checks": [],
        "evidence": [{"text": "unrelated detail"}] * 12,
    }
    side = IterativeDiagnoser.side(
        _snap({"deployment/reader": comp}), "deployment/reader", {"deployment/reader": result}
    )
    adoption = side.get("configuration_adoption")
    assert adoption and adoption[0]["source"] == "secret/connection"
    assert adoption[0]["updated_at"] == "2026-01-01T12:00:00Z"
    instructions = " ".join(build_tiebreak_questions(["a", "b"])["fault_start"].instructions["how_to_decide"])
    assert "write time alone does not prove" in instructions
    assert "Adoption alone does not establish correctness" in instructions
    assert "did not start the fault: undoing it would break" not in instructions
