"""jev_diag v2 diagnoser end to end with a stand-in decision model."""

from types import SimpleNamespace

from typesafe_sdk import ChoiceAnswer

from clients.jev_diag.classifier import CATEGORY_QUESTION, EVIDENCE_QUESTION
from clients.jev_diag.v2.agent import V2Diagnoser
from clients.jev_diag.v2.decide import QUESTION


def snapshot():
    proxy = {
        "kind": "Deployment",
        "name": "edge-proxy",
        "namespace": "shop",
        "healthy": False,
        "signals": ["CPU at its limit", "requests time out"],
        "template_changes": {
            "revision": "3",
            "changed_at": "2026-10-18T09:58:00+00:00",
            "changes": [
                "container envoy env FILTER_RULE added: rule-v2",
                "container envoy limits.cpu: unset -> 100m",
            ],
        },
    }
    return SimpleNamespace(
        app={"name": "shop", "namespaces": ["shop"]},
        components={"deployment/edge-proxy": proxy},
        cluster={"symptom_onset": "2026-10-18T10:00:00+00:00"},
    )


class Client:
    """Answers each pick with the option whose text contains `prefer` (or none of these)."""

    def __init__(self, prefer, fact=None, category=None):
        self.prefer, self.fact, self.category = prefer, fact, category

    def system_one(self, state, questions, **_):
        answers = {}
        for name, q in questions.items():
            want = {QUESTION: self.prefer, EVIDENCE_QUESTION: self.fact}.get(name) or ""
            texts = {item["id"]: item["text"] for item in state.get("evidence") or [] if isinstance(item, dict)}
            label = next((lbl for lbl, text in q.criteria.items() if want and want in texts.get(lbl, str(text))), None)
            if name == CATEGORY_QUESTION and self.category in q.criteria:
                label = self.category
            probs = {lbl: (0.9 if lbl == label else 0.1 / max(len(q.criteria) - 1, 1)) for lbl in q.criteria}
            answers[name] = ChoiceAnswer(choice=label or max(probs, key=probs.get), confidence=0.8, probabilities=probs)
        return SimpleNamespace(
            model="fake", usage=SimpleNamespace(input_tokens=100, output_tokens=0), answers=answers, choices=answers
        )


def test_the_picked_change_is_reported_with_its_value():
    diag = V2Diagnoser(client=Client("change 'container envoy env FILTER_RULE")).diagnose(snapshot())
    assert diag.component == "deployment/edge-proxy"
    assert "rule-v2" in diag.text and "Confidence: low" not in diag.text
    assert diag.investigation["rounds"][0]["route"] == "accept" and len(diag.investigation["rounds"]) == 1


def test_none_of_these_reads_detail_then_submits_a_partial_marked_uncertain():
    read = []
    diagnoser = V2Diagnoser(
        client=Client("None of these"), fetch_detail=lambda snap, cid: read.append(cid) or {"events": []}
    )
    diag = diagnoser.diagnose(snapshot())
    assert read == ["deployment/edge-proxy"]
    assert "Confidence: low" in diag.text  # still unsure, but it names a concrete fact (v1's key-evidence step)
    assert diag.investigation["pattern"] == "key-fact" and "not established" not in diag.text


def test_none_of_these_keeps_reading_components_not_read_yet():
    snap = snapshot()
    for name in ("cart", "consumer"):
        snap.components[f"deployment/{name}"] = {
            "kind": "Deployment",
            "name": name,
            "namespace": "shop",
            "healthy": False,
            "signals": ["pod phase Pending"],
        }
    snap.components["deployment/consumer"]["log_error_lines"] = 4
    read = []
    V2Diagnoser(client=Client("None of these"), fetch_detail=lambda s, cid: read.append(cid) or {}).diagnose(snap)
    assert sorted(read) == ["deployment/cart", "deployment/consumer", "deployment/edge-proxy"]
    assert len(read) == len(set(read)) and read[0] == "deployment/edge-proxy"  # most signals first, no rereads


def test_a_mechanism_unknown_answer_reads_the_component_and_reports_the_fact_the_model_picks():
    snap = snapshot()
    snap.components["deployment/edge-proxy"]["template_changes"] = None
    detail = {"workload": {"containers": [{"name": "sidecar", "command": ["nc", "-lk", "-p", "5000"]}]}}
    read = []
    client = Client("The fault is in this component", fact="container sidecar command")
    diag = V2Diagnoser(client=client, fetch_detail=lambda s, cid: read.append(cid) or detail).diagnose(snap)
    assert "Mechanism: container sidecar command: nc -lk -p 5000." in diag.text
    assert read == ["deployment/edge-proxy"] and diag.investigation["key_fact"].startswith("container sidecar")


def test_every_answer_gets_v1s_key_evidence_and_fault_type_and_the_linked_outside_object():
    snap = snapshot()
    snap.cluster["findings"] = [
        {
            "kind": "NetworkPolicy",
            "name": "deny",
            "namespace": "shop",
            "signal": "denies ingress",
            "components": ["deployment/edge-proxy"],
        }
    ]
    detail = {"workload": {"containers": [{"name": "envoy", "command": ["envoy", "-c", "/etc/envoy.yaml"]}]}}
    client = Client("The fault is in this component", fact="container envoy command", category="config_value")
    diag = V2Diagnoser(client=client, fetch_detail=lambda s, cid: detail).diagnose(snap)
    assert "Mechanism: container envoy command: envoy -c /etc/envoy.yaml." in diag.text
    assert (
        "Fault type: config value" in diag.text
        and "Fault object: networkpolicy `deny` in namespace `shop`" in diag.text
    )


def test_a_slow_pre_read_still_gets_one_decision_and_never_crashes(monkeypatch):
    import clients.jev_diag.v2.agent as agent_mod

    clock = [0.0]
    monkeypatch.setattr(agent_mod.time, "monotonic", lambda: clock[0])
    snap = snapshot()
    snap.components["deployment/edge-proxy"]["healthy"] = True

    def slow_read(s, cid):
        clock[0] += 121  # the read alone uses up the whole time budget
        return {}

    diag = V2Diagnoser(client=Client("change 'container envoy"), fetch_detail=slow_read).diagnose(snap)
    assert len(diag.investigation["rounds"]) == 1 and diag.text.startswith("Root cause")
