import json
from pathlib import Path

import pytest
import yaml

from sregym.service.khaos import KhaosController, KhaosUnsupportedError


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
            return json.dumps({"items": pods})
        if self.fail_ebpf_check and "/khaos/khaos --check" in command:
            raise RuntimeError("UNSUPPORTED: kernel helper unavailable")
        return ""

    def exec_command(self, command: str, input_data=None):
        self.commands.append(command)
        return ""

    def is_emulated_cluster(self):
        raise AssertionError("Khaos support must be probed on the nodes, not inferred from the cluster name")


def test_ebpf_support_is_checked_on_kind_nodes():
    kubectl = FakeKubectl()

    KhaosController(kubectl).ensure_deployed()

    checks = [command for command in kubectl.commands if "/khaos/khaos --check" in command]
    assert len(checks) == 2
    assert any("khaos-control-plane-abc" in command for command in checks)
    assert any("khaos-worker-abc" in command for command in checks)


def test_failed_ebpf_preflight_reports_node():
    kubectl = FakeKubectl(fail_ebpf_check=True)

    with pytest.raises(KhaosUnsupportedError, match="kind-control-plane.*eBPF"):
        KhaosController(kubectl).ensure_deployed()


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


def test_worker_daemonset_targets_unlabelled_worker_nodes():
    # kind and minikube workers carry no node-role label, so select by absence of the control-plane role.
    manifest = Path("sregym/service/khaos.yaml").read_text()
    worker = next(ds for ds in yaml.safe_load_all(manifest) if ds["metadata"]["name"] == "khaos-worker")
    pod_spec = worker["spec"]["template"]["spec"]
    terms = pod_spec["affinity"]["nodeAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"]["nodeSelectorTerms"]

    assert "nodeSelector" not in pod_spec
    assert terms == [
        {
            "matchExpressions": [
                {"key": "node-role.kubernetes.io/control-plane", "operator": "DoesNotExist"},
                {"key": "node-role.kubernetes.io/master", "operator": "DoesNotExist"},
            ]
        }
    ]
