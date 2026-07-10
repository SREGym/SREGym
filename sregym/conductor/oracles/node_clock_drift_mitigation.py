"""Mitigation oracle for node clock drift causing TLS failures."""

import contextlib
import time

from kubernetes import client
from kubernetes.client.rest import ApiException

from sregym.conductor.oracles.base import Oracle


class NodeClockDriftMitigationOracle(Oracle):
    # Verifying that node clock has been restored to cluster's time (real time)

    importance = 1.0

    def __init__(self, problem):
        super().__init__(problem)
        self.core_v1 = client.CoreV1Api()

    def evaluate(self) -> dict:
        print("== Node Clock Drift Mitigation Evaluation ==")
        results = {}
        try:
            if not hasattr(self.problem, "target_node") or self.problem.target_node is None:
                target_node = self._find_affected_node(self.problem.namespace)
                if not target_node:
                    print("Could not identify the affected node")
                    return {"success": False, "reason": "affected_node_not_found"}
            else:
                target_node = self.problem.target_node
            print(f"Checking target node: {target_node}")
            # Check 1: Clock is synchronized
            clock_skew = self._check_clock_skew(target_node)
            if clock_skew is None:
                print(f"Could not determine clock skew on {target_node}")
                return {"success": False, "clock_synchronized": False}
            max_skew_seconds = 60
            if abs(clock_skew) > max_skew_seconds:
                print(f"Clock on {target_node} still skewed by {clock_skew}s (> {max_skew_seconds}s threshold)")
                return {"success": False, "clock_synchronized": False, "clock_skew_seconds": clock_skew}
            print(f"Clock on {target_node} is synchronized (skew: {clock_skew}s)")
            results["clock_synchronized"] = True
            results["clock_skew_seconds"] = clock_skew
            # Check 2: Pods recovered
            pods_healthy = self._check_pod_health(self.problem.namespace, target_node)
            if not pods_healthy:
                print(f"Pods on {target_node} still show failures")
                return {"success": False, "pods_recovered": False}
            print(f"Pods on {target_node} have recovered")
            results["pods_recovered"] = True
            results["success"] = True
            results["affected_node"] = target_node
            print("Node clock drift mitigation successful")
            return results
        except Exception as e:
            print(f"Error during mitigation evaluation: {e}")
            return {"success": False, "error": str(e)}

    def _find_affected_node(self, namespace: str) -> str:
        try:
            pods = self.core_v1.list_namespaced_pod(namespace, label_selector="io.kompose.service=frontend").items
            for pod in pods:
                container_names = [c.name for c in pod.spec.containers]
                if pod.spec.node_name and "tls-health-check" in container_names:
                    return pod.spec.node_name
            return None
        except ApiException:
            return None

    def _check_clock_skew(self, node_name: str) -> int | None:
        """Calculate clock skew between conductor (control plane) and the node."""
        try:
            control_plane_time = int(time.time())
            node_time = self._get_node_time(node_name)
            if node_time is None:
                return None
            return node_time - control_plane_time
        except Exception as e:
            print(f"Error checking clock skew: {e}")
            return None

    def _get_node_time(self, node_name: str) -> int | None:
        """Get current time from the specified node via a privileged pod."""
        pod_name = f"time-check-{int(time.time() * 1000)}"
        pod_spec = {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {
                "name": pod_name,
                "namespace": "default",
                "labels": {"app": "time-checker"},
            },
            "spec": {
                "nodeSelector": {"kubernetes.io/hostname": node_name},
                "hostPID": True,
                "hostNetwork": True,
                "hostIPC": True,
                "terminationGracePeriodSeconds": 0,
                "automountServiceAccountToken": False,
                "restartPolicy": "Never",
                "containers": [
                    {
                        "name": "time-check",
                        "image": "ubuntu:22.04",
                        "imagePullPolicy": "IfNotPresent",
                        "command": ["sh", "-c"],
                        "args": ["nsenter --target 1 --mount --uts --ipc --net --pid -- date +%s"],
                        "securityContext": {"privileged": True},
                    }
                ],
            },
        }
        try:
            self.core_v1.create_namespaced_pod("default", pod_spec)
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                pod = self.core_v1.read_namespaced_pod(pod_name, "default")
                if pod.status.phase in ["Succeeded", "Failed"]:
                    break
                time.sleep(2)
            logs = self.core_v1.read_namespaced_pod_log(pod_name, "default")
            return int(logs.strip())
        except Exception as e:
            print(f"Error getting node time: {e}")
            return None
        finally:
            with contextlib.suppress(Exception):
                self.core_v1.delete_namespaced_pod(pod_name, "default", grace_period_seconds=0)

    def _check_pod_health(self, namespace: str, target_node: str) -> bool:
        """Check if pods on the target node are healthy with no TLS-related failures."""
        try:
            pods = self.core_v1.list_namespaced_pod(namespace).items
            node_pods = [pod for pod in pods if pod.spec.node_name == target_node]
            if not node_pods:
                print(f"No pods found on {target_node} — deployment may have been deleted")
                return False
            for pod in node_pods:
                if pod.status.phase not in ["Running", "Succeeded"]:
                    print(f"Pod {pod.metadata.name} is in phase {pod.status.phase}")
                    return False
                if pod.status.container_statuses:
                    for cs in pod.status.container_statuses:
                        if cs.state.waiting:
                            msg = (cs.state.waiting.message or "").lower()
                            if "certificate" in msg or "tls" in msg:
                                print(f"Pod {pod.metadata.name} has TLS error: {msg}")
                                return False
                        elif cs.state.terminated:
                            msg = (cs.state.terminated.message or "").lower()
                            reason = cs.state.terminated.reason or ""
                            if ("certificate" in msg or "tls" in msg) and reason != "Completed":
                                print(f"Pod {pod.metadata.name} terminated with TLS error: {msg}")
                                return False
            return True
        except Exception as e:
            print(f"Error checking pod health: {e}")
            return False
