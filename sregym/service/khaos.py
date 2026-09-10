import json
import os
import shlex
import subprocess
import time
from collections.abc import Iterable

import yaml

from sregym.paths import KHAOS_DS
from sregym.service.dm_flakey_manager import dm_flakey_preflight_script
from sregym.service.khaos_capabilities import KhaosCapability
from sregym.service.kubectl import KubeCtl

KHAOS_NS = "khaos"
KHAOS_DS_NAME = "khaos"


class KhaosUnsupportedError(RuntimeError):
    """Raised when a node kernel cannot provide a problem's Khaos capability."""


class KhaosImageError(RuntimeError):
    """The deployed Khaos binary does not implement the required interface."""


class KhaosController:
    def __init__(self, kubectl: KubeCtl):
        self.kubectl = kubectl

    def ensure_deployed(self, required_capabilities: Iterable[KhaosCapability] = ()):
        image = os.environ.get("KHAOS_IMAGE")
        pull_policy = os.environ.get("KHAOS_IMAGE_PULL_POLICY")
        if pull_policy and pull_policy not in {"Always", "IfNotPresent", "Never"}:
            raise ValueError("KHAOS_IMAGE_PULL_POLICY must be Always, IfNotPresent, or Never")
        self.kubectl.exec_command_checked(f"kubectl get ns {KHAOS_NS} >/dev/null 2>&1 || kubectl create ns {KHAOS_NS}")
        if image or pull_policy:
            manifests = list(yaml.safe_load_all(KHAOS_DS.read_text()))
            for manifest in manifests:
                container = manifest["spec"]["template"]["spec"]["containers"][0]
                if image:
                    container["image"] = image
                if pull_policy:
                    container["imagePullPolicy"] = pull_policy
            self.kubectl.exec_command_checked("kubectl apply -f -", input_data=yaml.safe_dump_all(manifests))
        else:
            self.kubectl.exec_command_checked(f"kubectl apply -f {KHAOS_DS}")

        # Wait for both DaemonSets to be ready (control-plane and worker)
        # The YAML file contains two DaemonSets: khaos-control-plane and khaos-worker
        self.kubectl.exec_command_checked(f"kubectl -n {KHAOS_NS} rollout status ds/khaos-control-plane --timeout=3m")
        self.kubectl.exec_command_checked(f"kubectl -n {KHAOS_NS} rollout status ds/khaos-worker --timeout=3m")
        self._check_capabilities(frozenset(required_capabilities))

    def _running_pods(self, profile: str | None = None) -> list[dict]:
        selector = "app=khaos"
        if profile:
            selector += f",profile={profile}"
        out = self.kubectl.exec_command_checked(f"kubectl -n {KHAOS_NS} get pods -l {shlex.quote(selector)} -o json")
        data = json.loads(out or "{}")
        return [item for item in data.get("items", []) if item.get("status", {}).get("phase") == "Running"]

    def _check_capabilities(self, required_capabilities: frozenset[KhaosCapability]) -> None:
        if not required_capabilities:
            return
        nodes = json.loads(self.kubectl.exec_command_checked("kubectl get nodes -o json"))["items"]
        for capability in sorted(required_capabilities, key=str):
            profile = (
                "worker"
                if capability in {KhaosCapability.DM_FLAKEY, KhaosCapability.DM_FLAKEY_RANDOM_CORRUPTION}
                else None
            )
            expected_nodes = {
                node["metadata"]["name"]
                for node in nodes
                if not profile
                or not {"node-role.kubernetes.io/control-plane", "node-role.kubernetes.io/master"}.intersection(
                    node["metadata"].get("labels", {})
                )
            }
            pods = self._running_pods(profile=profile)
            if not pods:
                target = "worker nodes" if profile else "cluster nodes"
                raise KhaosUnsupportedError(f"Khaos capability {capability.value!r} has no running pod on {target}")

            missing = expected_nodes - {pod.get("spec", {}).get("nodeName") for pod in pods}
            if missing:
                raise RuntimeError(
                    f"Khaos deployment is incomplete for {capability.value!r}: no running pod on {', '.join(sorted(missing))}"
                )

            for pod in pods:
                pod_name = pod["metadata"]["name"]
                node_name = pod.get("spec", {}).get("nodeName", "unknown")
                try:
                    self._check_pod_capability(pod_name, capability)
                except KhaosImageError:
                    raise
                except RuntimeError as exc:
                    detail = str(exc)
                    if isinstance(exc.__cause__, subprocess.CalledProcessError) and exc.__cause__.stderr:
                        stderr = exc.__cause__.stderr
                        detail = stderr.decode("utf-8", errors="replace") if isinstance(stderr, bytes) else stderr
                    raise KhaosUnsupportedError(
                        f"Node {node_name!r} does not support Khaos capability {capability.value!r}: {detail.strip()}"
                    ) from exc

    def _check_pod_capability(self, pod_name: str, capability: KhaosCapability) -> None:
        pod = shlex.quote(pod_name)
        if capability == KhaosCapability.EBPF_SYSCALL:
            try:
                self.kubectl.exec_command_checked(f"kubectl -n {KHAOS_NS} exec {pod} -- /khaos/khaos --check")
            except RuntimeError as exc:
                if "Usage:" in str(exc):
                    raise KhaosImageError(
                        "The deployed Khaos image does not support --check. "
                        "Build or publish the image containing Khaos PR #37, then select it with KHAOS_IMAGE."
                    ) from exc
                raise
            return

        if capability in {KhaosCapability.DM_FLAKEY, KhaosCapability.DM_FLAKEY_RANDOM_CORRUPTION}:
            script = dm_flakey_preflight_script(
                random_corruption=capability == KhaosCapability.DM_FLAKEY_RANDOM_CORRUPTION
            )
            self.kubectl.exec_command_checked(
                f"kubectl -n {KHAOS_NS} exec {pod} -- nsenter -t 1 -m -u -i -n -p "
                f"timeout --kill-after=5 30 sh -ec {shlex.quote(script)}",
                timeout=45,
            )
            return

        raise KhaosUnsupportedError(f"Unknown Khaos capability: {capability}")

    def teardown(self):
        self.kubectl.exec_command(f"kubectl delete ns {KHAOS_NS} --ignore-not-found")

    def _khaos_pod_on_node(self, node_name: str) -> str:
        deadline = time.time() + 90
        while time.time() < deadline:
            out = self.kubectl.exec_command(f"kubectl -n {KHAOS_NS} get pods -o json")
            if isinstance(out, tuple):
                out = out[0]
            data = json.loads(out or "{}")
            for item in data.get("items", []):
                if (
                    item.get("spec", {}).get("nodeName") == node_name
                    and item.get("status", {}).get("phase") == "Running"
                ):
                    return item["metadata"]["name"]
            time.sleep(3)
        # diagnostics
        ds = self.kubectl.exec_command(f"kubectl -n {KHAOS_NS} get ds -o wide")
        pods = self.kubectl.exec_command(f"kubectl -n {KHAOS_NS} get pods -o wide")
        raise RuntimeError(
            f"No running Khaos pod on node {node_name} after 90s.\n"
            f"DaemonSets:\n{ds[0] if isinstance(ds, tuple) else ds}\n"
            f"Pods:\n{pods[0] if isinstance(pods, tuple) else pods}"
        )

    def inject(self, node_name: str, fault_name: str, host_pid: int):
        """
        Run:  /khaos/khaos <fault_name> <pid>
        inside the Khaos pod on the specified node.
        """
        pod = self._khaos_pod_on_node(node_name)
        cmd = f"kubectl -n {KHAOS_NS} exec {pod} -- /khaos/khaos {fault_name} {host_pid}"
        out = self.kubectl.exec_command(cmd)
        return out

    def recover(self, node_name: str, fault_name: str):
        pod = self._khaos_pod_on_node(node_name)
        cmd = f"kubectl -n {KHAOS_NS} exec {pod} -- /khaos/khaos --recover {fault_name}"
        out = self.kubectl.exec_command(cmd)
        return out
