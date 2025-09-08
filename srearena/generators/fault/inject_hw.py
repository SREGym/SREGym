import json
import shlex
import subprocess
from typing import List, Tuple

from srearena.generators.fault.base import FaultInjector
from srearena.service.kubectl import KubeCtl


class HWFaultInjector(FaultInjector):
    """
    Fault injector that calls the Khaos DaemonSet to inject syscall-level faults
    against *host* PIDs corresponding to workload pods.
    """

    def __init__(self, khaos_namespace: str = "khaos", khaos_label: str = "app=khaos"):
        self.kubectl = KubeCtl()
        self.khaos_ns = khaos_namespace
        self.khaos_daemonset_label = khaos_label

    def inject(self, microservices: List[str], fault_type: str):
        for pod_ref in microservices:
            ns, pod = self._split_ns_pod(pod_ref)
            node = self._get_pod_node(ns, pod)
            container_id = self._get_container_id(ns, pod)
            host_pid = self._get_host_pid_on_node(node, container_id)
            self._exec_khaos_fault_on_node(node, fault_type, host_pid)

    def inject_node(self, namespace: str, fault_type: str, target_node: str = None):

        if target_node:
            selected_node = self._find_node_starting_with(target_node)
            if not selected_node:
                print(f"Node starting with '{target_node}' not found, selecting node with most pods")
                selected_node = self._find_node_with_most_pods(namespace)
        else:
            selected_node = self._find_node_with_most_pods(namespace)
        
        print(f"Selected target node: {selected_node}")
        
        target_pods = self._get_pods_on_node(namespace, selected_node)
        if not target_pods:
            raise RuntimeError(f"No running pods found on node '{selected_node}' in namespace '{namespace}'")
        
        print(f"Found {len(target_pods)} pods on node {selected_node}: {', '.join(target_pods)}")
        
        self.inject(target_pods, fault_type)
        return selected_node

    def recover_node(self, namespace: str, fault_type: str, target_node: str):
        target_pods = self._get_pods_on_node(namespace, target_node)
        if not target_pods:
            print(f"[warn] No pods found on node {target_node}; attempting best-effort recovery.")
            target_pods = []
        
        self.recover(target_pods, fault_type)

    def recover(self, microservices: List[str], fault_type: str):
        touched = set()
        for pod_ref in microservices:
            ns, pod = self._split_ns_pod(pod_ref)
            node = self._get_pod_node(ns, pod)
            if node in touched:
                continue
            self._exec_khaos_recover_on_node(node, fault_type)
            touched.add(node)

    def _split_ns_pod(self, ref: str) -> Tuple[str, str]:
        if "/" in ref:
            ns, pod = ref.split("/", 1)
        else:
            ns, pod = "default", ref
        return ns, pod

    def _jsonpath(self, ns: str, pod: str, path: str) -> str:
        cmd = f"kubectl -n {shlex.quote(ns)} get pod {shlex.quote(pod)} -o jsonpath='{path}'"
        out = self.kubectl.exec_command(cmd)
        if isinstance(out, tuple):
            out = out[0]
        return (out or "").strip()

    def _get_pod_node(self, ns: str, pod: str) -> str:
        node = self._jsonpath(ns, pod, "{.spec.nodeName}")
        if not node:
            raise RuntimeError(f"Pod {ns}/{pod} has no nodeName")
        return node

    def _get_container_id(self, ns: str, pod: str) -> str:
        # running container first
        cid = self._jsonpath(ns, pod, "{.status.containerStatuses[0].containerID}")
        if not cid:
            cid = self._jsonpath(ns, pod, "{.status.initContainerStatuses[0].containerID}")
        if not cid:
            raise RuntimeError(f"Pod {ns}/{pod} has no containerID yet (not running?)")
        if "://" in cid:
            cid = cid.split("://", 1)[1]
        return cid

    def _get_khaos_pod_on_node(self, node: str) -> str:
        cmd = f"kubectl -n {shlex.quote(self.khaos_ns)} get pods -l {shlex.quote(self.khaos_daemonset_label)} -o json"
        out = self.kubectl.exec_command(cmd)
        if isinstance(out, tuple):
            out = out[0]
        data = json.loads(out or "{}")
        for item in data.get("items", []):
            if item.get("spec", {}).get("nodeName") == node and item.get("status", {}).get("phase") == "Running":
                return item["metadata"]["name"]
        raise RuntimeError(f"No running Khaos DS pod found on node {node}")

    def _get_host_pid_on_node(self, node: str, container_id: str) -> int:
        pod_name = self._get_khaos_pod_on_node(node)

        # /proc scan (fast, works with hostPID:true)
        try:
            return self._get_host_pid_via_proc(pod_name, container_id)
        except Exception:
            pass

        # cgroup.procs search (works for both cgroup v1/v2)
        try:
            return self._get_host_pid_via_cgroups(pod_name, container_id)
        except Exception:
            pass

        raise RuntimeError(
            f"Failed to resolve host PID for container {container_id} on node {node} (proc, cgroups, cri all failed)"
        )

    def _get_host_pid_via_proc(self, khaos_pod: str, container_id: str) -> int:
        """
        Search host /proc/*/cgroup for the container ID and return the first PID.
        With hostPID:true, /proc is the host's proc.
        """
        short = shlex.quote(container_id[:12])
        cmd = [
            "kubectl",
            "-n",
            self.khaos_ns,
            "exec",
            khaos_pod,
            "--",
            "sh",
            "-lc",
            # grep cgroup entries for the container id; extract pid from path
            f"grep -l {short} /proc/*/cgroup 2>/dev/null | sed -n 's#.*/proc/\\([0-9]\\+\\)/cgroup#\\1#p' | head -n1",
        ]
        pid_txt = subprocess.check_output(cmd, stderr=subprocess.DEVNULL, text=True).strip()
        if pid_txt.isdigit():
            return int(pid_txt)

        # Try full ID if short didn't match
        fullq = shlex.quote(container_id)
        cmd[-1] = "sh -lc " + shlex.quote(
            f"grep -l {fullq} /proc/*/cgroup 2>/dev/null | sed -n 's#.*/proc/\\([0-9]\\+\\)/cgroup#\\1#p' | head -n1"
        )
        pid_txt = subprocess.check_output(cmd, stderr=subprocess.DEVNULL, text=True).strip()
        if pid_txt.isdigit():
            return int(pid_txt)

        raise RuntimeError("proc scan found no matching PID")

    def _detect_cgroup_root(self, khaos_pod: str) -> str:
        """
        Detect cgroup mount root (v2 unified vs v1). Returns a path under which cgroup.procs exists.
        """
        candidates = [
            "/sys/fs/cgroup",  # cgroup v2 (unified)
            "/sys/fs/cgroup/systemd",  # v1 systemd hierarchy
            "/sys/fs/cgroup/memory",  # v1 memory hierarchy
            "/sys/fs/cgroup/pids",  # v1 pids hierarchy
        ]
        for root in candidates:
            cmd = [
                "kubectl",
                "-n",
                self.khaos_ns,
                "exec",
                khaos_pod,
                "--",
                "sh",
                "-lc",
                f"test -d {shlex.quote(root)} && echo OK || true",
            ]
            out = subprocess.check_output(cmd, stderr=subprocess.DEVNULL, text=True).strip()
            if out == "OK":
                return root
        return "/sys/fs/cgroup"

    def _get_host_pid_via_cgroups(self, khaos_pod: str, container_id: str) -> int:
        """
        Search cgroup.procs files whose path contains the container ID; return a PID from that file.
        Works for both cgroup v1 and v2.
        """
        root = self._detect_cgroup_root(khaos_pod)
        short = shlex.quote(container_id[:12])
        cmd = [
            "kubectl",
            "-n",
            self.khaos_ns,
            "exec",
            khaos_pod,
            "--",
            "sh",
            "-lc",
            # find a cgroup.procs in any directory name/path that includes the short id; print first PID in that procs file
            f"find {shlex.quote(root)} -type f -name cgroup.procs -path '*{short}*' 2>/dev/null | head -n1 | xargs -r head -n1",
        ]
        pid_txt = subprocess.check_output(cmd, stderr=subprocess.DEVNULL, text=True).strip()
        if pid_txt.isdigit():
            return int(pid_txt)

        # Try with full ID
        fullq = shlex.quote(container_id)
        cmd[-1] = "sh -lc " + shlex.quote(
            f"find {root} -type f -name cgroup.procs -path '*{fullq}*' 2>/dev/null | head -n1 | xargs -r head -n1"
        )
        pid_txt = subprocess.check_output(cmd, stderr=subprocess.DEVNULL, text=True).strip()
        if pid_txt.isdigit():
            return int(pid_txt)

        raise RuntimeError("cgroup search found no matching PID")

    def _exec_khaos_fault_on_node(self, node: str, fault_type: str, host_pid: int):
        pod_name = self._get_khaos_pod_on_node(node)
        cmd = ["kubectl", "-n", self.khaos_ns, "exec", pod_name, "--", "/khaos/khaos", fault_type, str(host_pid)]
        subprocess.run(cmd, check=True)

    def _exec_khaos_recover_on_node(self, node: str, fault_type: str):
        pod_name = self._get_khaos_pod_on_node(node)
        cmd = ["kubectl", "-n", self.khaos_ns, "exec", pod_name, "--", "/khaos/khaos", "--recover", fault_type]
        subprocess.run(cmd, check=True)

    def _get_all_nodes(self) -> List[str]:
        """Get all node names in the cluster."""
        cmd = "kubectl get nodes -o jsonpath='{.items[*].metadata.name}'"
        out = self.kubectl.exec_command(cmd)
        if isinstance(out, tuple):
            out = out[0]
        nodes = (out or "").strip().split()
        return [node for node in nodes if node]

    def _find_node_starting_with(self, target_node: str) -> str:
        """Find a node that starts with the given string."""
        all_nodes = self._get_all_nodes()
        for node in all_nodes:
            if node.startswith(target_node):
                return node
        return None

    def _find_node_with_most_pods(self, namespace: str) -> str:
        """Find the node with the most pods in the namespace."""
        node_pod_count = {}
        
        cmd = f"kubectl -n {namespace} get pods -o json"
        out = self.kubectl.exec_command(cmd)
        if isinstance(out, tuple):
            out = out[0]
        try:
            data = json.loads(out)
            for item in data.get("items", []):
                phase = item.get("status", {}).get("phase")
                node_name = item.get("spec", {}).get("nodeName")
                if phase == "Running" and node_name:
                    node_pod_count[node_name] = node_pod_count.get(node_name, 0) + 1
        except Exception as e:
            print(f"Error getting pods: {e}")
            return None
        
        if not node_pod_count:
            raise RuntimeError(f"No running pods found in namespace '{namespace}'")
        
        selected_node = max(node_pod_count, key=node_pod_count.get)
        print(f"Node {selected_node} has {node_pod_count[selected_node]} pods")
        return selected_node

    def _get_pods_on_node(self, namespace: str, target_node: str) -> List[str]:
        """Get all pods in namespace on the target node."""
        pods: List[str] = []

        cmd = f"kubectl -n {namespace} get pods -o json"
        out = self.kubectl.exec_command(cmd)
        if isinstance(out, tuple):
            out = out[0]
        try:
            data = json.loads(out)
            for item in data.get("items", []):
                phase = item.get("status", {}).get("phase")
                node_name = item.get("spec", {}).get("nodeName")
                if phase == "Running" and node_name == target_node:
                    pods.append(f"{namespace}/{item['metadata']['name']}")
        except Exception as e:
            print(f"Error getting pods: {e}")

        return pods
