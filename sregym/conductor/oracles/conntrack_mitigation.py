import contextlib
import subprocess
import time

from kubernetes import client
from kubernetes.client.rest import ApiException

from sregym.conductor.oracles.mitigation import MitigationOracle

CONNTRACK_CMD = "cat /proc/sys/net/netfilter/nf_conntrack_count; cat /proc/sys/net/netfilter/nf_conntrack_max"
CONNTRACK_MAX_PATH = "/proc/sys/net/netfilter/nf_conntrack_max"


def read_node_conntrack_usage(kubectl, node_name: str, namespace: str = "default") -> tuple[int, int]:
    if node_name.startswith("kind-"):
        with contextlib.suppress(FileNotFoundError, subprocess.SubprocessError, RuntimeError):
            output = subprocess.run(
                ["docker", "exec", node_name, "sh", "-c", CONNTRACK_CMD],
                capture_output=True,
                check=True,
                text=True,
                timeout=10,
            ).stdout
            return _parse_conntrack_usage(node_name, output)

    output = _run_node_check_pod(kubectl, node_name, namespace, CONNTRACK_CMD)
    return _parse_conntrack_usage(node_name, output)


def write_node_conntrack_max(kubectl, node_name: str, value: int, namespace: str = "default") -> None:
    value = int(value)
    command = f"printf '%s' {value} > {CONNTRACK_MAX_PATH}"
    if node_name.startswith("kind-"):
        with contextlib.suppress(FileNotFoundError, subprocess.SubprocessError, RuntimeError):
            subprocess.run(
                ["docker", "exec", node_name, "sh", "-c", command],
                capture_output=True,
                check=True,
                text=True,
                timeout=10,
            )
            return

    _run_node_check_pod(kubectl, node_name, namespace, command, privileged=True)


def _parse_conntrack_usage(node_name: str, output: str) -> tuple[int, int]:
    values = [int(line.strip()) for line in output.splitlines() if line.strip().isdigit()]
    if len(values) < 2:
        raise RuntimeError(f"Could not parse conntrack usage from {node_name}: {output}")
    return values[0], values[1]


def _run_node_check_pod(kubectl, node_name: str, namespace: str, command: str, *, privileged: bool = False) -> str:
    core_v1 = getattr(kubectl, "core_v1_api", client.CoreV1Api())
    pod_name = f"node-healthcheck-{int(time.time() * 1000)}"
    container = {
        "name": "check",
        "image": "busybox:1.36",
        "command": ["sh", "-c", command],
    }
    if privileged:
        container["securityContext"] = {"privileged": True}
    pod = {
        "metadata": {"name": pod_name, "namespace": namespace, "labels": {"app": "node-healthcheck"}},
        "spec": {
            "restartPolicy": "Never",
            "automountServiceAccountToken": False,
            "hostNetwork": True,
            "nodeName": node_name,
            "containers": [container],
        },
    }
    try:
        core_v1.create_namespaced_pod(namespace, pod)
        phase = _wait_for_pod_completion(core_v1, pod_name, namespace)
        output = core_v1.read_namespaced_pod_log(pod_name, namespace)
        if phase != "Succeeded":
            raise RuntimeError(f"Node healthcheck pod {pod_name} on {node_name} finished in phase {phase}: {output}")
        return output
    finally:
        with contextlib.suppress(ApiException):
            core_v1.delete_namespaced_pod(pod_name, namespace, grace_period_seconds=0)


def _wait_for_pod_completion(core_v1, pod_name: str, namespace: str, timeout: int = 30) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        pod = core_v1.read_namespaced_pod(pod_name, namespace)
        if pod.status.phase in ("Succeeded", "Failed"):
            return pod.status.phase
        time.sleep(1)
    return "Pending"


class ConntrackMitigationOracle(MitigationOracle):
    def __init__(self, problem, ratio_threshold: float = 0.70, probe_attempts: int = 20, drain_timeout: int = 90):
        super().__init__(problem=problem)
        self.core_v1 = client.CoreV1Api()
        self.ratio_threshold = ratio_threshold
        self.probe_attempts = probe_attempts
        self.drain_timeout = drain_timeout

    def evaluate(self) -> dict:
        print("== Conntrack Mitigation Evaluation ==")
        results = super().evaluate()
        if not results.get("success"):
            return results

        node_name = self._victim_node()
        deadline = time.monotonic() + self.drain_timeout
        while True:
            count, maximum = read_node_conntrack_usage(self.problem.kubectl, node_name, self.problem.namespace)
            ratio = count / maximum if maximum else 1
            print(f"Node {node_name} conntrack usage: {count}/{maximum} ({ratio:.2%})")
            if ratio < self.ratio_threshold or time.monotonic() >= deadline:
                break
            time.sleep(5)

        probe_ok = self._frontend_probe_succeeds(node_name)

        results.update(
            {
                "conntrack_ratio": ratio,
                "frontend_probe": probe_ok,
                "success": ratio < self.ratio_threshold and probe_ok,
            }
        )
        return results

    def _victim_node(self) -> str:
        if getattr(self.problem, "victim_node", None):
            return self.problem.victim_node
        return self.problem.select_worker_nodes()[0]

    def _frontend_probe_succeeds(self, node_name: str) -> bool:
        pod_name = f"service-healthcheck-{int(time.time() * 1000)}"
        script = (
            f"ok=0; fail=0; for i in $(seq 1 {self.probe_attempts}); do "
            "wget -q -T 2 -O /dev/null http://frontend:5000/ && ok=$((ok+1)) || fail=$((fail+1)); "
            'sleep 0.1; done; echo "PROBE_OK=${ok} PROBE_FAIL=${fail}"; test "$fail" -le 1'
        )
        pod = {
            "metadata": {
                "name": pod_name,
                "namespace": self.problem.namespace,
                "labels": {"app": "service-healthcheck"},
            },
            "spec": {
                "restartPolicy": "Never",
                "automountServiceAccountToken": False,
                "nodeName": node_name,
                "containers": [{"name": "probe", "image": "busybox:1.36", "command": ["sh", "-c", script]}],
            },
        }
        try:
            self.core_v1.create_namespaced_pod(self.problem.namespace, pod)
            phase = self._wait_for_probe_pod(pod_name)
            logs = self.core_v1.read_namespaced_pod_log(pod_name, self.problem.namespace)
            print(logs.strip())
            return phase == "Succeeded"
        except ApiException:
            return False
        finally:
            with contextlib.suppress(ApiException):
                self.core_v1.delete_namespaced_pod(pod_name, self.problem.namespace, grace_period_seconds=0)

    def _wait_for_probe_pod(self, pod_name: str, timeout: int = 60) -> str:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            pod = self.core_v1.read_namespaced_pod(pod_name, self.problem.namespace)
            if pod.status.phase in ("Succeeded", "Failed"):
                return pod.status.phase
            time.sleep(1)
        return "Pending"
