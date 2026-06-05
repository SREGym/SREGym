import contextlib
import time

from kubernetes import client
from kubernetes.client.rest import ApiException

from sregym.conductor.oracles.mitigation import MitigationOracle


class PageCacheThrashMitigationOracle(MitigationOracle):
    """Mitigation oracle for the Hotel Reservation page-cache thrash problem.

    The problem is considered mitigated when the Hotel Reservation pods are
    healthy, the cache-thrashing batch workload is no longer co-located with the
    latency-sensitive frontend pod, and a simple frontend probe succeeds.
    """

    def __init__(self, problem, probe_attempts: int = 20):
        super().__init__(problem=problem)
        self.core_v1 = client.CoreV1Api()
        self.probe_attempts = probe_attempts

    def evaluate(self) -> dict:
        print("== Page Cache Thrash Mitigation Evaluation ==")

        results = super().evaluate()
        if not results.get("success"):
            return results

        frontend_node = self.problem.frontend_node or self.problem.find_frontend_node()
        thrasher_nodes = self._running_thrasher_nodes()

        if frontend_node in thrasher_nodes:
            return {
                "success": False,
                "reason": (
                    f"cache-thrashing workload '{self.problem.thrasher_deployment}' is still running "
                    f"on the frontend node '{frontend_node}'."
                ),
                "frontend_node": frontend_node,
                "thrasher_nodes": sorted(thrasher_nodes),
            }

        probe_ok = self._frontend_probe_succeeds(frontend_node)
        results.update(
            {
                "frontend_node": frontend_node,
                "thrasher_nodes": sorted(thrasher_nodes),
                "frontend_probe": probe_ok,
                "success": probe_ok,
            }
        )
        if not probe_ok:
            results["reason"] = "frontend probe failed after page-cache thrash mitigation."
        return results

    def _running_thrasher_nodes(self) -> set[str]:
        nodes = set()
        pods = self.core_v1.list_namespaced_pod(
            self.problem.namespace,
            label_selector=f"app={self.problem.thrasher_deployment}",
        ).items
        for pod in pods:
            if pod.status.phase == "Running" and pod.spec.node_name:
                nodes.add(pod.spec.node_name)
        return nodes

    def _frontend_probe_succeeds(self, node_name: str) -> bool:
        pod_name = f"page-cache-healthcheck-{int(time.time() * 1000)}"
        script = (
            f"ok=0; fail=0; for i in $(seq 1 {self.probe_attempts}); do "
            "wget -q -T 2 -O /dev/null http://frontend:5000/ && ok=$((ok+1)) || fail=$((fail+1)); "
            'sleep 0.1; done; echo "PROBE_OK=${ok} PROBE_FAIL=${fail}"; test "$fail" -le 1'
        )
        pod = {
            "metadata": {
                "name": pod_name,
                "namespace": self.problem.namespace,
                "labels": {"app": "page-cache-healthcheck"},
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
