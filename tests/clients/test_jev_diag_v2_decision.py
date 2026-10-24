"""jev_diag v2 decision step (tree.py): what is concluded from the examinations, and which questions are asked."""

from __future__ import annotations

from types import SimpleNamespace

from clients.jev_diag.v2.collector import ClusterSnapshot

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

        from clients.jev_diag.v2.classifier import COMPONENT_QUESTION

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
    from clients.jev_diag.v2.tree import IterativeDiagnoser

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
    from clients.jev_diag.v2.classifier import build_component_questions

    questions = build_component_questions(_snap({"deployment/a": _comp("a")}))
    assert set(questions) == {"root_cause_component", "fault_object_kind"}


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
    from clients.jev_diag.v2.classifier import ChoiceResult
    from clients.jev_diag.v2.tree import IterativeDiagnoser

    snap = _snap({"deployment/a": _comp("a"), "deployment/b": _comp("b")})
    triage = ChoiceResult(
        choice="deployment/b", confidence=0.7, probabilities={"deployment/b": 0.7, "deployment/a": 0.3}
    )
    diagnoser = IterativeDiagnoser(None, fetch_detail=lambda s, c: {})
    assert diagnoser.decide(snap, triage, {}, None, 0.0) == (
        "deployment/b",
        "triage top-1",
    )


def test_an_examination_sees_the_incident_it_has_to_explain():
    from clients.jev_diag.v2.tree import incident

    comps = {
        "deployment/web": _comp(
            "web",
            replicas={"desired": 2, "ready": 0},
            log_findings=[{"count": 7, "classes": ["connectivity or timeout"]}],
        ),
        "deployment/quiet": _comp("quiet", signals=[], healthy=True, replicas={"desired": 1, "ready": 1}),
        "deployment/collector": _comp("collector", role="observability", replicas={"desired": 1, "ready": 0}),
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
    from clients.jev_diag.v2.tree import incident

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


def test_objects_written_in_one_operation_with_a_hypothesis_become_hypotheses():
    from clients.jev_diag.v2.tree import OTHER_OPTION, written_together

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
    from clients.jev_diag.v2.derive import platform_findings

    comps = {
        "deployment/a": _comp("a", platform_failures={"pod_network": 6}),
        "deployment/b": _comp("b", platform_failures={"pod_network": 2}),
        "deployment/c": _comp("c", platform_failures={"preemption": 1}),
    }
    (finding,) = platform_findings(comps)  # one workload preempted is that workload's own business
    assert finding["kind"] == "Platform" and finding["name"] == "pod network" and finding["platform"]
    assert finding["components"] == ["deployment/a", "deployment/b"]
    assert "the events of 2 workloads state it" in finding["signal"]
