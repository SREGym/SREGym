import json
import shlex
import subprocess

from sregym.generators.fault.base import FaultInjector
from sregym.service.kubectl import KubeCtl


class HWFaultInjector(FaultInjector):
    """
    Fault injector that calls the Khaos DaemonSet to inject syscall-level faults
    against *host* PIDs corresponding to workload pods.
    """

    def __init__(self, khaos_namespace: str = "khaos", khaos_label: str = "app=khaos"):
        self.kubectl = KubeCtl()
        self.khaos_ns = khaos_namespace
        self.khaos_daemonset_label = khaos_label

    def inject(
        self,
        microservices: list[str],
        fault_type: str,
        params: list[str | int] | None = None,
    ):
        # Resolve and inject per-pod, tolerating individual failures. A single
        # pod racing with a container restart used to crash the entire problem
        # (and on the third deploy retry, the entire benchmark). As long as we
        # successfully inject into at least one pod, the fault is in effect.
        successes: list[str] = []
        failures: list[tuple[str, str]] = []
        for pod_ref in microservices:
            try:
                ns, pod = self._split_ns_pod(pod_ref)
                node = self._get_pod_node(ns, pod)
                container_id = self._get_container_id(ns, pod)
                host_pid = self._get_host_pid_on_node(node, container_id)
                self._exec_khaos_fault_on_node(node, fault_type, host_pid, params)
                successes.append(pod_ref)
            except Exception as e:
                failures.append((pod_ref, str(e)))
                print(f"[inject] Skipping {pod_ref}: {e}")

        if failures:
            print(
                f"[inject] {fault_type}: injected into {len(successes)}/"
                f"{len(successes) + len(failures)} pods ({len(failures)} skipped)"
            )
        if not successes:
            first = failures[0][1] if failures else "no pods provided"
            raise RuntimeError(
                f"inject({fault_type}): could not inject into any of "
                f"{len(microservices)} pod(s); first failure: {first}"
            )

    def inject_node(
        self,
        namespace: str,
        fault_type: str,
        target_node: str = None,
        params: list[str | int] | None = None,
    ):
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

        self.inject(target_pods, fault_type, params)
        return selected_node

    def recover_node(self, namespace: str, fault_type: str, target_node: str):
        target_pods = self._get_pods_on_node(namespace, target_node)
        if not target_pods:
            print(f"[warn] No pods found on node {target_node}; attempting best-effort recovery.")
            target_pods = []

        self.recover(target_pods, fault_type)

        # Sweep any leftover BPF pins for this fault. The reinjection monitor
        # pins a fresh probe per restarted container, but `khaos --recover` only
        # detaches one — leaving the rest as stale pins under /sys/fs/bpf that
        # accumulate across runs and eventually leak kernel resources. We
        # observed 14 leftover pins on node1 after a single failed
        # latent_sector_error run.
        try:
            self._cleanup_pinned_bpf_for_fault(target_node, fault_type)
        except Exception as e:
            print(f"[recover] BPF pin sweep on {target_node} failed (non-fatal): {e}")

    def _cleanup_pinned_bpf_for_fault(self, node: str, fault_type: str) -> None:
        """Remove leftover /sys/fs/bpf pins matching the given fault type. Best-effort."""
        pod_name = self._get_khaos_pod_on_node(node)
        # khaos pin names embed the fault name (e.g.
        # khaos-kprobe-lse-read-latent_sector_error_<pid>), so a substring glob
        # on the fault name catches all variants for this fault.
        pattern = f"/sys/fs/bpf/khaos-kprobe-*{fault_type}*"
        cmd = [
            "kubectl",
            "-n",
            self.khaos_ns,
            "exec",
            pod_name,
            "--",
            "sh",
            "-lc",
            f"set -e; n=$(ls {pattern} 2>/dev/null | wc -l); "
            f'if [ "$n" -gt 0 ]; then rm -f {pattern}; fi; echo SWEPT=$n',
        ]
        try:
            out = subprocess.check_output(cmd, stderr=subprocess.STDOUT, text=True, timeout=15)
            for line in out.splitlines():
                if line.startswith("SWEPT="):
                    n = line.split("=", 1)[1].strip()
                    if n.isdigit() and int(n) > 0:
                        print(f"[recover] Swept {n} leftover BPF pin(s) for {fault_type} on {node}")
                    break
        except subprocess.CalledProcessError as e:
            print(f"[recover] Pin sweep command failed on {node}: {e.output}")
        except subprocess.TimeoutExpired:
            print(f"[recover] Pin sweep timed out on {node}")

    def recover(self, microservices: list[str], fault_type: str):
        touched = set()
        for pod_ref in microservices:
            ns, pod = self._split_ns_pod(pod_ref)
            node = self._get_pod_node(ns, pod)
            if node in touched:
                continue
            self._exec_khaos_recover_on_node(node, fault_type)
            touched.add(node)

    def _split_ns_pod(self, ref: str) -> tuple[str, str]:
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
        errors: list[str] = []

        # /proc scan (fast, works with hostPID:true). This is the primary path.
        try:
            return self._get_host_pid_via_proc(pod_name, container_id)
        except Exception as e:
            errors.append(f"proc: {e}")

        # cgroup.procs search. The khaos daemonset mounts the host's
        # /sys/fs/cgroup at /host/sys/fs/cgroup (read-only), so this can find
        # workload container cgroups even when the /proc grep races with a
        # restart and misses.
        try:
            return self._get_host_pid_via_cgroups(pod_name, container_id)
        except Exception as e:
            errors.append(f"cgroups: {e}")

        raise RuntimeError(
            f"Failed to resolve host PID for container {container_id} on node {node}: " + "; ".join(errors)
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

        Prefers /host/sys/fs/cgroup (the host's cgroup hierarchy mounted by
        khaos.yaml) so we can see workload container cgroups, not just the
        khaos container's own namespaced view.
        """
        candidates = [
            "/host/sys/fs/cgroup",  # host cgroup root mounted by khaos.yaml (preferred)
            "/host/sys/fs/cgroup/systemd",
            "/host/sys/fs/cgroup/memory",
            "/host/sys/fs/cgroup/pids",
            "/sys/fs/cgroup",  # fallback: container's own (works only if no cgroup ns)
            "/sys/fs/cgroup/systemd",
            "/sys/fs/cgroup/memory",
            "/sys/fs/cgroup/pids",
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

    def _exec_khaos_fault_on_node(
        self,
        node: str,
        fault_type: str,
        host_pid: int,
        params: list[str | int] | None = None,
    ):
        pod_name = self._get_khaos_pod_on_node(node)
        cmd = [
            "kubectl",
            "-n",
            self.khaos_ns,
            "exec",
            pod_name,
            "--",
            "/khaos/khaos",
            fault_type,
            str(host_pid),
        ]
        if params:
            cmd.extend(str(p) for p in params)
        subprocess.run(cmd, check=True)

    def _exec_khaos_recover_on_node(self, node: str, fault_type: str):
        pod_name = self._get_khaos_pod_on_node(node)
        cmd = ["kubectl", "-n", self.khaos_ns, "exec", pod_name, "--", "/khaos/khaos", "--recover", fault_type]
        subprocess.run(cmd, check=True)

    def _get_all_nodes(self) -> list[str]:
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

    def _get_pods_on_node(self, namespace: str, target_node: str) -> list[str]:
        """Get all pods in namespace on the target node."""
        pods: list[str] = []

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
