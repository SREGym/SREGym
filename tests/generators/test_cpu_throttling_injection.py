from types import SimpleNamespace

import pytest
import yaml

from sregym.generators.fault.inject_virtual import VirtualizationFaultInjector


def _deployment_yaml(name, uid, cpu_limit="1"):
    return {
        "metadata": {"name": name, "uid": uid},
        "spec": {
            "template": {
                "spec": {
                    "containers": [
                        {
                            "name": f"{name}-container",
                            "resources": {
                                "requests": {"cpu": "100m", "memory": "32Mi"},
                                "limits": {"cpu": cpu_limit, "memory": "64Mi"},
                            },
                        }
                    ]
                }
            }
        },
    }


class _PatchKubeCtl:
    def __init__(self):
        self.patches = []

    def patch_deployment(self, name, namespace, body):
        self.patches.append((name, namespace, body))


def _injector(monkeypatch, tmp_path, deployments):
    injector = object.__new__(VirtualizationFaultInjector)
    injector.namespace = "hotel-reservation"
    injector.kubectl = _PatchKubeCtl()
    monkeypatch.setattr(
        injector,
        "_cpu_resource_snapshot_path",
        lambda service: tmp_path / f"{service}_cpu_resources.yaml",
    )
    monkeypatch.setattr(injector, "_get_all_deployments", lambda: list(deployments))
    monkeypatch.setattr(injector, "calibrate_all_limits", lambda **kwargs: {name: "200m" for name in deployments})
    monkeypatch.setattr(injector, "_get_deployment_yaml", lambda service: deployments[service])
    monkeypatch.setattr(injector, "_wait_for_deployment_rollout", lambda *args, **kwargs: None)
    return injector


def test_injection_patches_only_target_and_explicit_decoys(monkeypatch, tmp_path):
    deployments = {
        "geo": _deployment_yaml("geo", "uid-geo"),
        "frontend": _deployment_yaml("frontend", "uid-frontend"),
        "mongodb-geo": _deployment_yaml("mongodb-geo", "uid-mongodb-geo"),
    }
    injector = _injector(monkeypatch, tmp_path, deployments)

    injected = injector.inject_cpu_throttle(["geo"], additional_services=["frontend"])

    assert injected == {"geo": "200m", "frontend": "200m"}
    assert [name for name, _, _ in injector.kubectl.patches] == ["geo", "frontend"]

    geo_resources = injector.kubectl.patches[0][2]["spec"]["template"]["spec"]["containers"][0]["resources"]
    assert geo_resources["requests"]["cpu"] == "200m"
    assert geo_resources["limits"]["cpu"] == "200m"
    assert geo_resources["limits"]["memory"] == "64Mi"
    assert geo_resources["$patch"] == "replace"

    frontend_resources = injector.kubectl.patches[1][2]["spec"]["template"]["spec"]["containers"][0]["resources"]
    assert frontend_resources["requests"]["cpu"] == "200m"
    assert frontend_resources["limits"]["cpu"] == "200m"

    snapshot = yaml.safe_load((tmp_path / "geo_cpu_resources.yaml").read_text())
    assert snapshot["containers"][0]["resources"]["limits"]["cpu"] == "1"


def test_repeated_injection_preserves_the_first_snapshot_for_same_deployment(monkeypatch, tmp_path):
    deployments = {"geo": _deployment_yaml("geo", "uid-geo")}
    injector = _injector(monkeypatch, tmp_path, deployments)
    injector.inject_cpu_throttle(["geo"], cpu_limit="125m")

    deployments["geo"]["spec"]["template"]["spec"]["containers"][0]["resources"]["limits"]["cpu"] = "125m"
    injector.inject_cpu_throttle(["geo"], cpu_limit="125m")

    snapshot = yaml.safe_load((tmp_path / "geo_cpu_resources.yaml").read_text())
    assert snapshot["containers"][0]["resources"]["limits"]["cpu"] == "1"


def test_partial_injection_rolls_back_every_service_already_patched(monkeypatch, tmp_path):
    deployments = {
        "geo": _deployment_yaml("geo", "uid-geo"),
        "frontend": _deployment_yaml("frontend", "uid-frontend"),
    }
    injector = _injector(monkeypatch, tmp_path, deployments)
    restored = []
    monkeypatch.setattr(injector, "_restore_cpu_resources", restored.append)

    patch_count = 0

    def patch(name, namespace, body):
        nonlocal patch_count
        patch_count += 1
        if patch_count == 2:
            raise RuntimeError("API unavailable")

    injector.kubectl.patch_deployment = patch

    with pytest.raises(RuntimeError, match="API unavailable"):
        injector.inject_cpu_throttle(["geo"], all_services=True)

    assert restored == ["geo"]


def test_verification_remeasures_after_bumping_a_noisy_service(monkeypatch):
    injector = object.__new__(VirtualizationFaultInjector)
    injector.namespace = "hotel-reservation"
    injector.kubectl = SimpleNamespace()

    monkeypatch.setattr(injector, "_wait_for_services_ready", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        injector,
        "_get_ready_pods",
        lambda services: {service: f"{service}-pod" for service in services},
    )
    samples = iter(
        [
            {"geo": 0.40, "mongodb-reservation": 0.20},
            {"geo": 0.42, "mongodb-reservation": 0.04},
        ]
    )
    monkeypatch.setattr(injector, "_measure_throttle_rates", lambda *args, **kwargs: next(samples))
    bumps = []
    monkeypatch.setattr(injector, "_set_cpu_limit", lambda service, limit: bumps.append((service, limit)))
    monkeypatch.setattr("sregym.generators.fault.inject_virtual.time.sleep", lambda _: None)

    injected = injector.verify_injection(
        {"geo": "125m", "mongodb-reservation": "60m"},
        ["geo"],
        settle_seconds=0,
        sample_seconds=0,
    )

    assert bumps == [("mongodb-reservation", "90m")]
    assert injected["mongodb-reservation"] == "90m"


def test_verification_fails_when_target_never_throttles(monkeypatch):
    injector = object.__new__(VirtualizationFaultInjector)
    injector.namespace = "hotel-reservation"
    injector.kubectl = SimpleNamespace()

    monkeypatch.setattr(injector, "_wait_for_services_ready", lambda *args, **kwargs: None)
    monkeypatch.setattr(injector, "_get_ready_pods", lambda services: {"geo": "geo-pod"})
    monkeypatch.setattr(injector, "_measure_throttle_rates", lambda *args, **kwargs: {"geo": 0.01})
    monkeypatch.setattr("sregym.generators.fault.inject_virtual.time.sleep", lambda _: None)

    with pytest.raises(RuntimeError, match="target throttle"):
        injector.verify_injection(
            {"geo": "125m"},
            ["geo"],
            settle_seconds=0,
            sample_seconds=0,
            max_attempts=2,
        )


def test_calibration_is_fresh_for_every_injection(monkeypatch):
    injector = object.__new__(VirtualizationFaultInjector)
    injector.namespace = "hotel-reservation"
    injector.kubectl = SimpleNamespace()

    monkeypatch.setattr(injector, "_get_ready_pods", lambda services: {"geo": "geo-pod"})
    readings = iter(
        [
            {"geo": {"usage_usec": 0, "nr_periods": 0, "nr_throttled": 0}},
            {"geo": {"usage_usec": 10_000, "nr_periods": 1, "nr_throttled": 0}},
            {"geo": {"usage_usec": 10_000, "nr_periods": 1, "nr_throttled": 0}},
            {"geo": {"usage_usec": 40_000, "nr_periods": 2, "nr_throttled": 0}},
        ]
    )
    read_count = 0

    def read_stats(_pods):
        nonlocal read_count
        read_count += 1
        return next(readings)

    monkeypatch.setattr(injector, "_read_cpu_stats", read_stats)
    monkeypatch.setattr("sregym.generators.fault.inject_virtual.time.sleep", lambda _: None)

    first = injector.calibrate_all_limits(
        ["geo"],
        ["geo"],
        warmup_seconds=0,
        n_samples=1,
        sample_window_seconds=0,
    )
    second = injector.calibrate_all_limits(
        ["geo"],
        ["geo"],
        warmup_seconds=0,
        n_samples=1,
        sample_window_seconds=0,
    )

    assert first == {"geo": "115m"}
    assert second == {"geo": "345m"}
    assert read_count == 4


def test_cpu_stat_retries_transient_exec_timeouts(monkeypatch):
    injector = object.__new__(VirtualizationFaultInjector)
    injector.namespace = "hotel-reservation"
    attempts = 0

    def exec_checked(command, timeout):
        nonlocal attempts
        attempts += 1
        assert "kubectl --request-timeout=1s exec geo-pod" in command
        assert timeout == 11
        if attempts < 3:
            raise RuntimeError("Command timed out")
        return "usage_usec 100\nnr_periods 2\nnr_throttled 1\n"

    injector.kubectl = SimpleNamespace(exec_command_checked=exec_checked)
    monkeypatch.setattr("sregym.generators.fault.inject_virtual.time.sleep", lambda _: None)

    result = injector._read_cpu_stat("geo-pod", max_attempts=3, request_timeout=1)

    assert result == {"usage_usec": 100, "nr_periods": 2, "nr_throttled": 1}
    assert attempts == 3


def test_cpu_stat_fails_after_bounded_retries(monkeypatch):
    injector = object.__new__(VirtualizationFaultInjector)
    injector.namespace = "hotel-reservation"
    attempts = 0

    def exec_checked(command, timeout):
        nonlocal attempts
        attempts += 1
        raise RuntimeError("Command timed out")

    injector.kubectl = SimpleNamespace(exec_command_checked=exec_checked)
    monkeypatch.setattr("sregym.generators.fault.inject_virtual.time.sleep", lambda _: None)

    with pytest.raises(RuntimeError, match="after 2 attempts"):
        injector._read_cpu_stat("geo-pod", max_attempts=2, request_timeout=1)

    assert attempts == 2
