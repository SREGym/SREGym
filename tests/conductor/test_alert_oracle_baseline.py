"""Unit tests for AlertOracle's pre-injection alert baseline (SREGym#745).

The astronomy-shop Grafana sidecar trips ``ContainerCPUThrottling`` chronically,
which previously made AlertOracle fail mitigation for every astronomy-shop problem
regardless of the agent. AlertOracle now snapshots alerts already firing before the
fault is injected and ignores them, while still catching alerts the agent newly
triggers (e.g. the unmitigated injected fault).
"""

import json
from types import SimpleNamespace

import sregym.conductor.oracles.alert_oracle as alert_oracle_module
from sregym.conductor.oracles.alert_oracle import AlertOracle

NAMESPACE = "astronomy-shop"


def _alerts_payload(*alertnames):
    """Build a Prometheus /api/v1/alerts response with the given firing alerts."""
    return json.dumps(
        {
            "data": {
                "alerts": [
                    {
                        "state": "firing",
                        "labels": {"namespace": NAMESPACE, "alertname": name},
                    }
                    for name in alertnames
                ]
            }
        }
    )


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
    assert oracle._baseline_alertnames == {"ContainerCPUThrottling"}

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

    assert oracle._baseline_alertnames is None
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
