import builtins
import tempfile
from types import SimpleNamespace

import yaml

from sregym.generators.fault.inject_virtual import VirtualizationFaultInjector


class _RecordingKubeCtl:
    def __init__(self):
        self.commands = []

    def exec_command(self, command):
        self.commands.append(command)
        return "OK\n"


class _InjectionKubeCtl:
    def __init__(self):
        self.events = []
        self.apps_v1_api = self

    def exec_command(self, command):
        self.events.append(("command", command))
        return "OK\n"

    def read_namespaced_deployment(self, name, namespace):
        self.events.append(("read", f"{namespace}/{name}"))
        return SimpleNamespace(
            metadata=SimpleNamespace(generation=1),
            spec=SimpleNamespace(replicas=3),
            status=SimpleNamespace(
                observed_generation=1,
                updated_replicas=3,
                ready_replicas=3,
                available_replicas=3,
                unavailable_replicas=0,
            ),
        )


def test_rolling_update_recovery_uses_saved_original_and_waits_for_readiness():
    injector = object.__new__(VirtualizationFaultInjector)
    injector.namespace = "social-network"
    injector.kubectl = _RecordingKubeCtl()

    injector.recover_rolling_update_misconfigured(["custom-service"])

    assert injector.kubectl.commands == [
        "kubectl delete deployment custom-service -n social-network",
        "kubectl apply -f /tmp/custom-service-orig.yaml -n social-network",
        "kubectl rollout status deployment/custom-service -n social-network --timeout=120s",
    ]


def test_rolling_update_injection_waits_for_healthy_baseline_before_fault_patch(monkeypatch, tmp_path):
    real_named_temporary_file = tempfile.NamedTemporaryFile
    monkeypatch.setattr(
        tempfile,
        "NamedTemporaryFile",
        lambda *args, **kwargs: real_named_temporary_file(*args, dir=tmp_path, **kwargs),
    )

    real_open = builtins.open

    def redirected_open(path, *args, **kwargs):
        if path == "/tmp/custom-service-orig.yaml":
            path = tmp_path / "custom-service-orig.yaml"
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", redirected_open)

    injector = object.__new__(VirtualizationFaultInjector)
    injector.namespace = "social-network"
    injector.kubectl = _InjectionKubeCtl()

    injector.inject_rolling_update_misconfigured(["custom-service"])

    apply_index = next(
        index
        for index, event in enumerate(injector.kubectl.events)
        if event[0] == "command" and event[1].startswith("kubectl apply")
    )
    ready_index = next(index for index, event in enumerate(injector.kubectl.events) if event[0] == "read")
    patch_index = next(
        index
        for index, event in enumerate(injector.kubectl.events)
        if event[0] == "command" and event[1].startswith("kubectl patch")
    )

    assert apply_index < ready_index < patch_index

    patch_command = injector.kubectl.events[patch_index][1]
    patch_path = patch_command.split("--patch-file ", 1)[1]
    with real_open(patch_path) as patch_file:
        patch = yaml.safe_load(patch_file)
    init_container = patch["spec"]["template"]["spec"]["initContainers"][0]
    assert init_container["image"] == "busybox:1.36"
    assert init_container["imagePullPolicy"] == "IfNotPresent"
