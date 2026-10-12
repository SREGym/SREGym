import io
import json
from copy import deepcopy
from unittest.mock import Mock

import pytest

from sregym.service.apps.mattermost.runtime import capacity_scaler as scaler
from sregym.service.apps.mattermost.runtime import chat_gateway as gateway


def pod(index):
    return {
        "metadata": {"uid": f"gateway-{index}"},
        "status": {
            "phase": "Running",
            "podIP": f"10.0.0.{index}",
            "conditions": [{"type": "Ready", "status": "True"}],
        },
    }


@pytest.fixture
def clock(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(scaler.time, "monotonic", lambda: now[0])
    return now


@pytest.fixture
def rules():
    return {**scaler.DEFAULT_POLICY, "enabled": True}


def test_gateway_distinguishes_unsampled_cpu_from_measured_zero(monkeypatch):
    cpu = {}
    monkeypatch.setattr(gateway, "cpu", cpu)
    monkeypatch.setattr(gateway, "control", lambda name, default: default)

    assert "cpu_percent" not in gateway.snapshot()
    cpu["percent"] = 0.0
    assert gateway.snapshot()["cpu_percent"] == 0.0


@pytest.mark.parametrize(
    "state",
    [
        "healthy",
        "partial",
        "surge",
        "deleting",
        "old-terminating",
        "old-failed",
        "old-succeeded",
        "unready",
        "pending",
        "no-ip",
        "scrape-error",
    ],
)
def test_metrics_require_the_complete_ready_population(monkeypatch, state):
    pods = [pod(index) for index in range(1, 4)]
    if state == "partial":
        pods.pop()
    elif state in ("surge", "old-terminating", "old-failed", "old-succeeded"):
        pods.append(pod(4))
    if state in ("deleting", "old-terminating"):
        pods[-1]["metadata"]["deletionTimestamp"] = "2026-10-11T00:00:00Z"
    if state == "unready":
        pods[-1]["status"]["conditions"][0]["status"] = "False"
    if state == "pending":
        pods[-1]["status"]["phase"] = "Pending"
    if state in ("old-failed", "old-succeeded"):
        pods[-1]["status"]["phase"] = "Failed" if state == "old-failed" else "Succeeded"
    if state == "no-ip":
        pods[-1]["status"].pop("podIP")
    monkeypatch.setattr(scaler, "api", Mock(return_value={"items": pods}))

    def urlopen(url, timeout):
        assert timeout == 5
        if state == "scrape-error" and url == "http://10.0.0.3:8080/metrics":
            raise OSError("connection reset")
        return io.BytesIO(json.dumps({"cpu_percent": 0.0}).encode())

    monkeypatch.setattr(scaler.urllib.request, "urlopen", urlopen)
    samples = scaler.gateway_metrics(3)
    if state in ("healthy", "old-terminating", "old-failed", "old-succeeded"):
        assert samples == [{"cpu_percent": 0.0, "pod_uid": f"gateway-{index}"} for index in range(1, 4)]
    else:
        assert samples == []


@pytest.mark.parametrize("value", [None, True, "0", -1, float("nan"), float("inf"), float("-inf")])
def test_invalid_metric_is_unknown_not_idle(value):
    assert scaler.observe([{"cpu_percent": 10}, {"cpu_percent": value}], "cpu_percent") is None


def test_observation_requires_every_pod_but_accepts_real_zero():
    assert scaler.observe([], "cpu_percent") is None
    assert scaler.observe([{"cpu_percent": 10}, {}], "cpu_percent") is None
    assert scaler.observe([{"cpu_percent": 0}, {"cpu_percent": 2.5}], "cpu_percent") == 1.25


def test_scale_in_requires_continuous_low_usage(clock, rules):
    stabilizer = scaler.ScaleInStabilizer()
    population = ["a", "b", "c"]
    assert stabilizer.target(3, 2, population, rules) == 3
    clock[0] = scaler.SCALE_IN_STABILIZATION_SECONDS - 1
    assert stabilizer.target(3, 2, population, rules) == 3
    assert stabilizer.target(3, 3, population, rules) == 3
    clock[0] = scaler.SCALE_IN_STABILIZATION_SECONDS
    assert stabilizer.target(3, 2, population, rules) == 3
    clock[0] += scaler.SCALE_IN_STABILIZATION_SECONDS
    assert stabilizer.target(3, 2, list(reversed(population)), rules) == 2


@pytest.mark.parametrize("change", ["population", "replicas", "policy"])
def test_scale_in_window_restarts_after_context_changes(clock, rules, change):
    stabilizer = scaler.ScaleInStabilizer()
    population, current = ["a", "b", "c"], 3
    stabilizer.target(current, current - 1, population, rules)
    clock[0] = scaler.SCALE_IN_STABILIZATION_SECONDS
    if change == "population":
        population[-1] = "replacement"
    elif change == "replicas":
        current = 4
        population.append("d")
    else:
        rules["scale_in_below"] = 30

    assert stabilizer.target(current, current - 1, population, rules) == current
    clock[0] += scaler.SCALE_IN_STABILIZATION_SECONDS
    assert stabilizer.target(current, current - 1, population, rules) == current - 1


def test_scale_out_does_not_wait_for_scale_in_window(clock, rules):
    stabilizer = scaler.ScaleInStabilizer()
    stabilizer.target(3, 2, ["a", "b", "c"], rules)
    assert stabilizer.target(3, 4, ["a", "b", "c"], rules) == 4
    assert stabilizer.context is None


@pytest.fixture
def reconcile_env(monkeypatch, rules):
    scale = {"metadata": {"resourceVersion": "27"}, "spec": {"replicas": 3}}
    api = Mock(return_value=scale)
    samples = [{"pod_uid": str(index), "cpu_percent": 0.0} for index in range(3)]
    metrics = Mock(return_value=samples)
    record = Mock()
    monkeypatch.setattr(scaler, "policy", lambda: deepcopy(rules))
    monkeypatch.setattr(scaler, "api", api)
    monkeypatch.setattr(scaler, "gateway_metrics", metrics)
    monkeypatch.setattr(scaler, "record", record)
    return api, metrics, record


def test_reconcile_scale_in_uses_resource_version_after_stabilization(clock, reconcile_env):
    api, metrics, record = reconcile_env
    stabilizer = scaler.ScaleInStabilizer()
    scaler.reconcile(stabilizer)
    assert api.call_count == 1
    assert "waiting for stable measurements" in record.call_args.args[0]["decision"]
    clock[0] = scaler.SCALE_IN_STABILIZATION_SECONDS
    scaler.reconcile(stabilizer)

    metrics.assert_called_with(3)
    path = f"/apis/apps/v1/namespaces/{scaler.NAMESPACE}/deployments/{scaler.TARGET}/scale"
    api.assert_called_with(path, "PATCH", {"metadata": {"resourceVersion": "27"}, "spec": {"replicas": 2}})
    assert record.call_args.args[0]["replicas"] == 2


@pytest.mark.parametrize("interruption", ["no-samples", "no-cpu", "disabled"])
def test_reconcile_resets_low_usage_window_on_unknown_or_disabled(clock, rules, reconcile_env, interruption):
    api, metrics, record = reconcile_env
    stabilizer = scaler.ScaleInStabilizer()
    scaler.reconcile(stabilizer)
    clock[0] = scaler.SCALE_IN_STABILIZATION_SECONDS
    original = metrics.return_value
    if interruption == "no-samples":
        metrics.return_value = []
    elif interruption == "no-cpu":
        metrics.return_value = [{"pod_uid": str(index)} for index in range(3)]
    else:
        rules["enabled"] = False
    scaler.reconcile(stabilizer)
    assert stabilizer.context is None
    assert record.call_args.args[0]["observed"] is None
    assert all(call.args[1:] == () for call in api.call_args_list)

    metrics.return_value = original
    rules["enabled"] = True
    scaler.reconcile(stabilizer)
    assert record.call_args.args[0]["replicas"] == 3
    clock[0] += scaler.SCALE_IN_STABILIZATION_SECONDS
    scaler.reconcile(stabilizer)
    assert record.call_args.args[0]["replicas"] == 2


def test_reconcile_scales_out_immediately(clock, reconcile_env):
    api, metrics, record = reconcile_env
    for sample in metrics.return_value:
        sample["cpu_percent"] = 90
    scaler.reconcile(scaler.ScaleInStabilizer())
    assert api.call_args.args[1:] == ("PATCH", {"metadata": {"resourceVersion": "27"}, "spec": {"replicas": 4}})
    assert record.call_args.args[0]["replicas"] == 4


def test_loop_resets_low_usage_window_after_api_error(monkeypatch, clock, reconcile_env):
    api, metrics, record = reconcile_env
    real_reconcile = scaler.reconcile
    stabilizers, iterations = [], []

    def reconcile(stabilizer):
        stabilizers.append(stabilizer)
        return real_reconcile(stabilizer)

    def sleep(seconds):
        iterations.append(seconds)
        clock[0] += scaler.SCALE_IN_STABILIZATION_SECONDS
        if len(iterations) == 1:
            api.side_effect = OSError("apiserver unavailable")
        elif len(iterations) == 2:
            assert stabilizers[-1].context is None
            api.side_effect = None
        else:
            raise KeyboardInterrupt

    monkeypatch.setattr(scaler, "reconcile", reconcile)
    monkeypatch.setattr(scaler.time, "sleep", sleep)
    with pytest.raises(KeyboardInterrupt):
        scaler.loop()
    assert len({id(stabilizer) for stabilizer in stabilizers}) == 1
    assert record.call_args_list[1].args[0]["decision"] == "error: OSError: apiserver unavailable"
    assert record.call_args.args[0]["replicas"] == 3
    assert all(call.args[1:] == () for call in api.call_args_list)
