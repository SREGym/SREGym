import json
import subprocess
from pathlib import Path

import pytest
import yaml

from sregym.conductor.problems.silent_data_corruption import SilentDataCorruption
from sregym.service.khaos import KhaosController, KhaosImageError, KhaosUnsupportedError
from sregym.service.khaos_capabilities import KhaosCapability


class FakeKubectl:
    def __init__(self, *, fail_ebpf_check: bool = False):
        self.commands: list[str] = []
        self.fail_ebpf_check = fail_ebpf_check
        self.applied_manifest = None

    def exec_command_checked(self, command: str, input_data=None, timeout=None):
        self.commands.append(command)
        if command == "kubectl apply -f -":
            self.applied_manifest = input_data
        if command == "kubectl get nodes -o json":
            return json.dumps(
                {
                    "items": [
                        {
                            "metadata": {
                                "name": "kind-control-plane",
                                "labels": {"node-role.kubernetes.io/control-plane": ""},
                            }
                        },
                        {"metadata": {"name": "kind-worker", "labels": {}}},
                    ]
                }
            )
        if "get pods" in command:
            pods = [
                {
                    "metadata": {"name": "khaos-control-plane-abc"},
                    "spec": {"nodeName": "kind-control-plane"},
                    "status": {"phase": "Running"},
                },
                {
                    "metadata": {"name": "khaos-worker-abc"},
                    "spec": {"nodeName": "kind-worker"},
                    "status": {"phase": "Running"},
                },
            ]
            if "profile=worker" in command:
                pods = pods[1:]
            return json.dumps({"items": pods})
        if self.fail_ebpf_check and "/khaos/khaos --check" in command:
            raise RuntimeError("UNSUPPORTED: kernel helper unavailable")
        return ""

    def exec_command(self, command: str, input_data=None):
        self.commands.append(command)
        return ""

    def is_emulated_cluster(self):
        raise AssertionError("Khaos support must be capability-based, not cluster-name-based")


def test_ebpf_capability_is_checked_on_kind_nodes():
    kubectl = FakeKubectl()

    KhaosController(kubectl).ensure_deployed({KhaosCapability.EBPF_SYSCALL})

    checks = [command for command in kubectl.commands if "/khaos/khaos --check" in command]
    assert len(checks) == 2
    assert any("khaos-control-plane-abc" in command for command in checks)
    assert any("khaos-worker-abc" in command for command in checks)


def test_failed_ebpf_preflight_reports_node_and_capability():
    kubectl = FakeKubectl(fail_ebpf_check=True)

    with pytest.raises(KhaosUnsupportedError, match="kind-control-plane.*ebpf-syscall"):
        KhaosController(kubectl).ensure_deployed({KhaosCapability.EBPF_SYSCALL})


def test_dm_flakey_preflight_only_targets_worker_nodes():
    kubectl = FakeKubectl()

    KhaosController(kubectl).ensure_deployed({KhaosCapability.DM_FLAKEY})

    checks = [command for command in kubectl.commands if "modprobe dm_flakey" in command]
    assert len(checks) == 1
    assert "khaos-worker-abc" in checks[0]
    assert "dmsetup targets" in checks[0]


def test_daemonsets_bootstrap_bpffs_with_privileged_xlab_image():
    manifest = Path("sregym/service/khaos.yaml").read_text()
    daemonsets = list(yaml.safe_load_all(manifest))

    assert len(daemonsets) == 2
    for daemonset in daemonsets:
        pod_spec = daemonset["spec"]["template"]["spec"]
        container = pod_spec["containers"][0]
        assert pod_spec["hostPID"] is True
        assert container["securityContext"]["privileged"] is True
        assert container["image"] == "ghcr.io/xlab-uiuc/khaos:latest"
        assert container["imagePullPolicy"] == "Always"
        assert "mount -t bpf" in container["args"][0]


def test_silent_data_corruption_requests_dm_flakey_instead_of_ebpf():
    assert SilentDataCorruption.khaos_capabilities(object()) == frozenset({KhaosCapability.DM_FLAKEY_RANDOM_CORRUPTION})


def test_missing_worker_cannot_pass_ebpf_preflight():
    class MissingWorker(FakeKubectl):
        def exec_command_checked(self, command, **kwargs):
            out = super().exec_command_checked(command, **kwargs)
            if "get pods" in command:
                return json.dumps({"items": json.loads(out)["items"][:1]})
            return out

    with pytest.raises(RuntimeError, match="deployment is incomplete.*kind-worker"):
        KhaosController(MissingWorker()).ensure_deployed({KhaosCapability.EBPF_SYSCALL})


def test_old_image_is_not_reported_as_unsupported_kernel():
    class OldImage(FakeKubectl):
        def exec_command_checked(self, command, **kwargs):
            if "/khaos/khaos --check" in command:
                raise RuntimeError("Usage: /khaos/khaos <fault_name> <pid>")
            return super().exec_command_checked(command, **kwargs)

    with pytest.raises(KhaosImageError, match="does not support --check"):
        KhaosController(OldImage()).ensure_deployed({KhaosCapability.EBPF_SYSCALL})


def test_random_corruption_preflight_loads_required_features():
    kubectl = FakeKubectl()
    KhaosController(kubectl).ensure_deployed({KhaosCapability.DM_FLAKEY_RANDOM_CORRUPTION})
    probe = next(c for c in kubectl.commands if "modprobe dm_flakey" in c)
    assert "random_read_corrupt" in probe and "random_write_corrupt" in probe
    assert "dmsetup reload --noudevsync" in probe
    assert "timeout --kill-after=5 30" in probe


def test_worker_manifest_supports_unlabelled_kind_nodes_and_fresh_storage():
    _, worker = yaml.safe_load_all(Path("sregym/service/khaos.yaml").read_text())
    spec = worker["spec"]["template"]["spec"]
    assert not spec.get("nodeSelector")
    expressions = spec["affinity"]["nodeAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"][
        "nodeSelectorTerms"
    ][0]["matchExpressions"]
    assert {e["key"] for e in expressions} == {
        "node-role.kubernetes.io/control-plane",
        "node-role.kubernetes.io/master",
    }
    assert all(e["operator"] == "DoesNotExist" for e in expressions)
    volume = next(v for v in spec["volumes"] if v["name"] == "openebs")
    assert volume["hostPath"]["type"] == "DirectoryOrCreate"


def test_local_pr_image_override_applies_to_both_daemonsets(monkeypatch):
    monkeypatch.setenv("KHAOS_IMAGE", "khaos:pr37-test")
    monkeypatch.setenv("KHAOS_IMAGE_PULL_POLICY", "Never")
    kubectl = FakeKubectl()
    KhaosController(kubectl).ensure_deployed()
    manifests = list(yaml.safe_load_all(kubectl.applied_manifest))
    assert len(manifests) == 2
    for manifest in manifests:
        container = manifest["spec"]["template"]["spec"]["containers"][0]
        assert container["image"] == "khaos:pr37-test"
        assert container["imagePullPolicy"] == "Never"


def test_invalid_pull_policy_fails_before_cluster_changes(monkeypatch):
    monkeypatch.setenv("KHAOS_IMAGE_PULL_POLICY", "Sometimes")
    kubectl = FakeKubectl()
    with pytest.raises(ValueError, match="KHAOS_IMAGE_PULL_POLICY"):
        KhaosController(kubectl).ensure_deployed()
    assert not kubectl.commands


def test_preflight_error_reports_stderr_without_embedding_the_probe_script():
    class FailedFeature(FakeKubectl):
        def exec_command_checked(self, command, **kwargs):
            if "modprobe dm_flakey" in command:
                cause = subprocess.CalledProcessError(1, command, stderr=b"random corruption is unsupported")
                raise RuntimeError(f"failed command: {command}") from cause
            return super().exec_command_checked(command, **kwargs)

    with pytest.raises(KhaosUnsupportedError) as error:
        KhaosController(FailedFeature()).ensure_deployed({KhaosCapability.DM_FLAKEY_RANDOM_CORRUPTION})
    assert "kind-worker" in str(error.value)
    assert "random corruption is unsupported" in str(error.value)
    assert "modprobe" not in str(error.value)
