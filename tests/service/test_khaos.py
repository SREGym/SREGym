import json
from pathlib import Path

import pytest
import yaml

from sregym.conductor.problems.silent_data_corruption import SilentDataCorruption
from sregym.service.khaos import KhaosController, KhaosUnsupportedError
from sregym.service.khaos_capabilities import KhaosCapability


class FakeKubectl:
    def __init__(self, *, fail_ebpf_check: bool = False):
        self.commands: list[str] = []
        self.fail_ebpf_check = fail_ebpf_check

    def exec_command_checked(self, command: str, input_data=None, timeout=None):
        self.commands.append(command)
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
    assert SilentDataCorruption.khaos_capabilities(object()) == frozenset({KhaosCapability.DM_FLAKEY})
