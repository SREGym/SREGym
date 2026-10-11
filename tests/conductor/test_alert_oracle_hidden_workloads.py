"""AlertOracle can ignore alerts about workloads hidden from the agent.

SREGym's load generators carry labels the agent visibility policy hides, so an
agent can neither see nor fix them; with ``ignore_hidden_workloads`` their
alerts do not decide the grade. Alerts about the application still do.
"""

import json
from types import SimpleNamespace

import sregym.conductor.oracles.alert_oracle as alert_oracle_module
from sregym.conductor.oracles.alert_oracle import AlertOracle

NAMESPACE = "blueprint-hotel-reservation"

WORKLOADS = [
    {"kind": "Deployment", "metadata": {"name": "bhotelwrk-wlgen", "labels": {"job": "workload"}}},
    {
        "kind": "ReplicaSet",
        "metadata": {"name": "bhotelwrk-wlgen-fff79b648", "labels": {"job": "workload"}},
    },
    {"kind": "Pod", "metadata": {"name": "bhotelwrk-wlgen-fff79b648-5mbsk", "labels": {"job": "workload"}}},
    {"kind": "Deployment", "metadata": {"name": "frontend", "labels": {"app": "frontend"}}},
    {"kind": "ReplicaSet", "metadata": {"name": "frontend-6d9f7c5b8", "labels": {"app": "frontend"}}},
]


def _alert(name, **labels):
    return {"state": "firing", "labels": {"namespace": NAMESPACE, "alertname": name, **labels}}


def _oracle(monkeypatch, alerts, *, ignore_hidden_workloads=True):
    def check_output(cmd, **kwargs):
        if cmd[:2] == ["kubectl", "get"]:
            return json.dumps({"items": WORKLOADS})
        return json.dumps({"data": {"alerts": alerts}})

    monkeypatch.setattr(alert_oracle_module.subprocess, "check_output", check_output)
    return AlertOracle(problem=SimpleNamespace(namespace=NAMESPACE), ignore_hidden_workloads=ignore_hidden_workloads)


def _firing(oracle):
    return sorted(
        (a["labels"]["alertname"], a["labels"].get("pod") or a["labels"].get("deployment", ""))
        for a in oracle._query_firing_alerts(NAMESPACE)
    )


def test_alerts_about_hidden_workloads_are_ignored(monkeypatch):
    oracle = _oracle(
        monkeypatch,
        [
            _alert("KubePodNotReady", pod="bhotelwrk-wlgen-fff79b648-5mbsk"),
            _alert("DeploymentNotReady", deployment="bhotelwrk-wlgen"),
            # Already replaced: named after the hidden ReplicaSet.
            _alert("ContainerCPUThrottling", pod="bhotelwrk-wlgen-fff79b648-x7k2p"),
        ],
    )
    assert _firing(oracle) == []


def test_alerts_about_the_application_still_count(monkeypatch):
    oracle = _oracle(
        monkeypatch,
        [
            _alert("KubePodNotReady", pod="frontend-6d9f7c5b8-abcde"),
            _alert("HighRequestErrorRate", service_name="frontend"),
            _alert("DeploymentNotReady", deployment="bhotelwrk-wlgen"),
        ],
    )
    assert _firing(oracle) == [("HighRequestErrorRate", ""), ("KubePodNotReady", "frontend-6d9f7c5b8-abcde")]


def test_a_name_that_only_starts_like_a_hidden_workload_counts(monkeypatch):
    oracle = _oracle(monkeypatch, [_alert("KubePodNotReady", pod="bhotelwrk-wlgen-fff79b648-proxy-abcde")])
    assert _firing(oracle) == [("KubePodNotReady", "bhotelwrk-wlgen-fff79b648-proxy-abcde")]


def test_hidden_workload_alerts_count_by_default(monkeypatch):
    oracle = _oracle(
        monkeypatch,
        [_alert("KubePodNotReady", pod="bhotelwrk-wlgen-fff79b648-5mbsk")],
        ignore_hidden_workloads=False,
    )
    assert _firing(oracle) == [("KubePodNotReady", "bhotelwrk-wlgen-fff79b648-5mbsk")]


def test_unlisted_workloads_leave_every_alert_counting(monkeypatch):
    oracle = _oracle(monkeypatch, [_alert("KubePodNotReady", pod="bhotelwrk-wlgen-fff79b648-5mbsk")])

    def check_output(cmd, **kwargs):
        if cmd[:2] == ["kubectl", "get"]:
            raise alert_oracle_module.subprocess.CalledProcessError(1, cmd)
        return json.dumps({"data": {"alerts": [_alert("KubePodNotReady", pod="bhotelwrk-wlgen-fff79b648-5mbsk")]}})

    monkeypatch.setattr(alert_oracle_module.subprocess, "check_output", check_output)
    assert _firing(oracle) == [("KubePodNotReady", "bhotelwrk-wlgen-fff79b648-5mbsk")]
