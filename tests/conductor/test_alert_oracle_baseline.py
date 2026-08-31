"""Unit tests for AlertOracle's pre-injection alert baseline (SREGym#745).

The astronomy-shop Grafana sidecar trips ``ContainerCPUThrottling`` chronically,
which previously made AlertOracle fail mitigation for every astronomy-shop problem
regardless of the agent. AlertOracle now snapshots alerts already firing before the
fault is injected and ignores them, while still catching alerts the agent newly
triggers (e.g. the unmitigated injected fault).

Baseline matching is keyed on the full alert label set (not just ``alertname``),
so a pre-existing alert on one pod/service doesn't mask a newly firing alert with
the same name on a different pod/service (see PR #933 review).
"""

import json
from types import SimpleNamespace

import sregym.conductor.oracles.alert_oracle as alert_oracle_module
from sregym.conductor.oracles.alert_oracle import AlertOracle

NAMESPACE = "astronomy-shop"


def _alerts_payload(*alerts):
    """Build a Prometheus /api/v1/alerts response.

    Each entry in *alerts* is either a plain alertname string (labeled only with
    ``namespace``/``alertname``), or an ``(alertname, extra_labels_dict)`` tuple
    for alerts that need additional labels such as ``pod``.
    """
    entries = []
    for alert in alerts:
        if isinstance(alert, tuple):
            name, extra_labels = alert
        else:
            name, extra_labels = alert, {}
        labels = {"namespace": NAMESPACE, "alertname": name, **extra_labels}
        entries.append({"state": "firing", "labels": labels})
    return json.dumps({"data": {"alerts": entries}})


def _oracle(monkeypatch, payload_box):
    """An AlertOracle whose Prometheus query returns ``payload_box['payload']``."""
    oracle = AlertOracle(problem=SimpleNamespace(namespace=NAMESPACE))
    monkeypatch.setattr(
        alert_oracle_module.subprocess,
        "check_output",
        lambda *a, **k: payload_box["payload"],
    )
    return oracle


def _firing_names(oracle):
    return sorted(a["labels"]["alertname"] for a in oracle._query_firing_alerts(NAMESPACE))


def test_baseline_ignores_preexisting_chronic_alert(monkeypatch):
    # Chronic environmental noise is firing before fault injection.
    box = {"payload": _alerts_payload("ContainerCPUThrottling")}
    oracle = _oracle(monkeypatch, box)

    oracle.capture_baseline()
    assert len(oracle._baseline_instances) == 1

    # The agent mitigated the fault; only the chronic noise remains -> no firing.
    assert _firing_names(oracle) == []


def test_newly_triggered_alert_still_caught(monkeypatch):
    # Baseline captured with only the chronic noise present.
    box = {"payload": _alerts_payload("ContainerCPUThrottling")}
    oracle = _oracle(monkeypatch, box)
    oracle.capture_baseline()

    # Agent left the injected fault unmitigated -> its alert is new since baseline.
    box["payload"] = _alerts_payload("ContainerCPUThrottling", "TargetDown")
    assert _firing_names(oracle) == ["TargetDown"]


def test_no_baseline_preserves_original_behavior(monkeypatch):
    # Without capture_baseline, every namespace alert is reported (legacy behavior).
    box = {"payload": _alerts_payload("ContainerCPUThrottling", "TargetDown")}
    oracle = _oracle(monkeypatch, box)

    assert oracle._baseline_instances is None
    assert _firing_names(oracle) == ["ContainerCPUThrottling", "TargetDown"]


def test_explicit_exclude_alerts_still_applied(monkeypatch):
    box = {"payload": _alerts_payload("HighRequestRate", "TargetDown")}
    oracle = AlertOracle(
        problem=SimpleNamespace(namespace=NAMESPACE),
        exclude_alerts=["HighRequestRate"],
    )
    monkeypatch.setattr(
        alert_oracle_module.subprocess,
        "check_output",
        lambda *a, **k: box["payload"],
    )
    assert _firing_names(oracle) == ["TargetDown"]


def test_same_alertname_new_pod_is_not_masked_by_baseline(monkeypatch):
    """PR #933 review: baseline must match the full instance, not just alertname.

    ``ContainerCPUThrottling`` firing on pod-a is captured in the baseline. The
    same alertname later fires on pod-b, caused by the injected fault. Since
    pod-b is a different instance, it must still be reported even though an
    alert with the same name is in the baseline.
    """
    box = {"payload": _alerts_payload(("ContainerCPUThrottling", {"pod": "pod-a"}))}
    oracle = _oracle(monkeypatch, box)
    oracle.capture_baseline()

    # Pre-existing instance on pod-a is still firing, plus a *new* instance of the
    # same alertname on pod-b caused by the fault.
    box["payload"] = _alerts_payload(
        ("ContainerCPUThrottling", {"pod": "pod-a"}),
        ("ContainerCPUThrottling", {"pod": "pod-b"}),
    )
    firing = oracle._query_firing_alerts(NAMESPACE)
    assert len(firing) == 1
    assert firing[0]["labels"]["pod"] == "pod-b"


def test_same_alertname_same_pod_stays_masked(monkeypatch):
    """The exact same instance (same labels) that was in the baseline stays ignored."""
    box = {"payload": _alerts_payload(("ContainerCPUThrottling", {"pod": "pod-a"}))}
    oracle = _oracle(monkeypatch, box)
    oracle.capture_baseline()

    # Same instance still firing, nothing new.
    assert oracle._query_firing_alerts(NAMESPACE) == []
