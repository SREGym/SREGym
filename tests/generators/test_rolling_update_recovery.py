import builtins
import os
import tempfile
from types import SimpleNamespace

import pytest
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
                replicas=3,
                observed_generation=1,
                updated_replicas=3,
                ready_replicas=3,
                available_replicas=3,
                unavailable_replicas=0,
            ),
        )


def test_rolling_update_recovery_uses_saved_original_and_waits_for_readiness(monkeypatch):
    # Recovery checks that the baseline manifest exists before applying it.
    # This test only cares about the command sequence, so pretend that one
    # file is there instead of writing to the real /tmp.
    real_exists = os.path.exists
    monkeypatch.setattr(
        os.path,
        "exists",
        lambda path: True if path == "/tmp/custom-service-orig.yaml" else real_exists(path),
    )

    injector = object.__new__(VirtualizationFaultInjector)
    injector.namespace = "social-network"
    injector.kubectl = _RecordingKubeCtl()

    injector.recover_rolling_update_misconfigured(["custom-service"])

    assert injector.kubectl.commands == [
        "kubectl delete deployment custom-service -n social-network",
        "kubectl apply -f /tmp/custom-service-orig.yaml -n social-network",
        "kubectl rollout status deployment/custom-service -n social-network --timeout=120s",
    ]


def test_rolling_update_recovery_raises_when_the_baseline_manifest_is_missing(monkeypatch):
    """Recovery raises FileNotFoundError naming /tmp/<service>-orig.yaml
    when the file is missing. It still deletes the faulty deployment.
    """
    real_exists = os.path.exists
    monkeypatch.setattr(
        os.path,
        "exists",
        lambda path: False if path == "/tmp/custom-service-orig.yaml" else real_exists(path),
    )

    injector = object.__new__(VirtualizationFaultInjector)
    injector.namespace = "social-network"
    kube = _RecordingKubeCtl()
    injector.kubectl = kube

    with pytest.raises(FileNotFoundError, match="custom-service-orig.yaml"):
        injector.recover_rolling_update_misconfigured(["custom-service"])

    # The faulty deployment is still deleted. That part of cleanup does not
    # depend on the missing file.
    assert kube.commands == ["kubectl delete deployment custom-service -n social-network"]


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

    # Injection also removes this path up front. Redirect that too, so the
    # test never touches the real /tmp.
    real_remove = os.remove

    def redirected_remove(path, *args, **kwargs):
        if path == "/tmp/custom-service-orig.yaml":
            path = tmp_path / "custom-service-orig.yaml"
        return real_remove(path, *args, **kwargs)

    monkeypatch.setattr(os, "remove", redirected_remove)

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


def test_injection_clears_a_stale_baseline_manifest_before_the_rollout(monkeypatch, tmp_path):
    """A leftover /tmp/<service>-orig.yaml from an earlier run passes the
    recovery existence check and gets applied against the wrong cluster
    state. Injection removes it before the baseline rollout starts, so a
    stale file never survives into this run's recovery.
    """
    stale_path = tmp_path / "custom-service-orig.yaml"
    stale_path.write_text("stale: true\n")

    real_remove = os.remove

    def redirected_remove(path, *args, **kwargs):
        if path == "/tmp/custom-service-orig.yaml":
            path = stale_path
        return real_remove(path, *args, **kwargs)

    monkeypatch.setattr(os, "remove", redirected_remove)

    # Make the baseline rollout time out right after the stale file is
    # removed. The test only has to prove the unlink happened up front.
    injector = object.__new__(VirtualizationFaultInjector)
    injector.namespace = "social-network"
    injector.kubectl = _RecordingKubeCtl()
    monkeypatch.setattr(injector, "_wait_for_deployment_rollout", lambda *a, **k: (_ for _ in ()).throw(TimeoutError))

    real_named_temporary_file = tempfile.NamedTemporaryFile
    monkeypatch.setattr(
        tempfile,
        "NamedTemporaryFile",
        lambda *args, **kwargs: real_named_temporary_file(*args, dir=tmp_path, **kwargs),
    )

    assert stale_path.exists()

    with pytest.raises(TimeoutError):
        injector.inject_rolling_update_misconfigured(["custom-service"])

    assert not stale_path.exists()
