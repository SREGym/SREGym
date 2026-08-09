"""Mitigation oracle for the InternalTrafficPolicyLocal problem.

Passes when **either**:
* ``spec.internalTrafficPolicy`` on the ``recommendation`` Service is no longer
  ``Local`` (i.e. changed to ``Cluster`` or the field was removed), **or**
* Every worker node has at least one Ready ``recommendation`` Service endpoint
  (making ``Local`` safe because no caller is left without a usable local
  backend).

After the policy/topology check clears, a TCP connectivity probe (busybox
``nc``) is run from a worker node that had no local pod during injection to
confirm in-cluster traffic actually flows.
"""

import contextlib
import time

from kubernetes import client
from kubernetes.client.rest import ApiException

from sregym.conductor.oracles.base import Oracle


class InternalTrafficPolicyMitigationOracle(Oracle):
    importance = 1.0

    def __init__(self, problem):
        super().__init__(problem)
        self.core_v1 = client.CoreV1Api()
        self.discovery_v1 = client.DiscoveryV1Api()

    def evaluate(self) -> dict:
        print("== InternalTrafficPolicy Mitigation Evaluation ==")

        try:
            svc = self.core_v1.read_namespaced_service(self.problem.FAULTY_SERVICE, self.problem.namespace)
        except ApiException as exc:
            print(f"Could not read service: {exc}")
            return {"success": False}

        policy = (svc.spec.internal_traffic_policy or "Cluster").strip()
        print(f"service/{self.problem.FAULTY_SERVICE} internalTrafficPolicy={policy}")

        worker_nodes = self.problem.worker_nodes()
        nodes_with_endpoint = self._nodes_with_ready_endpoint()
        uncovered_nodes = [n for n in worker_nodes if n not in nodes_with_endpoint]

        print(f"Worker nodes: {worker_nodes}")
        print(f"Nodes with Ready service endpoint: {sorted(nodes_with_endpoint)}")
        print(f"Uncovered nodes: {uncovered_nodes}")

        if policy == "Local" and uncovered_nodes:
            print(
                f"Fault still active: internalTrafficPolicy=Local and "
                f"{len(uncovered_nodes)} worker node(s) have no Ready local endpoint."
            )
            return {
                "success": False,
                "internalTrafficPolicy": policy,
                "uncovered_nodes": uncovered_nodes,
            }

        probe_node = self._pick_probe_node(worker_nodes, nodes_with_endpoint)
        print(f"Running connectivity probe from node: {probe_node}")
        probe_ok = self._connectivity_probe(probe_node)

        print(f"Probe result: {'PASS' if probe_ok else 'FAIL'}")
        return {
            "success": probe_ok,
            "internalTrafficPolicy": policy,
            "probe_node": probe_node,
            "probe_ok": probe_ok,
        }

    def _nodes_with_ready_endpoint(self) -> set[str]:
        """Return nodes containing an endpoint the Service can actually use."""
        slices = self.discovery_v1.list_namespaced_endpoint_slice(
            self.problem.namespace,
            label_selector=f"kubernetes.io/service-name={self.problem.FAULTY_SERVICE}",
        )
        return {
            endpoint.node_name
            for endpoint_slice in slices.items
            for endpoint in (endpoint_slice.endpoints or [])
            if endpoint.node_name and endpoint.conditions is not None and endpoint.conditions.ready is True
        }

    def _pick_probe_node(self, worker_nodes: list[str], nodes_with_endpoint: set[str]) -> str:
        """Prefer victim_node, then first uncovered node, then last worker."""
        victim = getattr(self.problem, "victim_node", None)
        if victim and victim in worker_nodes:
            return victim
        uncovered = [n for n in worker_nodes if n not in nodes_with_endpoint]
        if uncovered:
            return uncovered[0]
        return worker_nodes[-1]

    def _connectivity_probe(self, node_name: str, timeout: int = 60) -> bool:
        """TCP probe to the faulty service ClusterIP from the given node."""
        namespace = self.problem.namespace
        svc_host = f"{self.problem.FAULTY_SERVICE}.{namespace}.svc.cluster.local"
        port = self.problem.SERVICE_PORT
        script = f"nc -z -w 5 {svc_host} {port} && echo PROBE_OK || {{ echo PROBE_FAIL; exit 1; }}"
        pod_name = f"svc-probe-{int(time.time() * 1000)}"
        pod = {
            "metadata": {"name": pod_name, "namespace": namespace, "labels": {"app": "svc-probe"}},
            "spec": {
                "restartPolicy": "Never",
                "automountServiceAccountToken": False,
                "nodeName": node_name,
                "containers": [{"name": "probe", "image": "busybox:1.36", "command": ["sh", "-c", script]}],
            },
        }
        try:
            self.core_v1.create_namespaced_pod(namespace, pod)
            phase = self._wait_for_pod_completion(pod_name, namespace, timeout)
            logs = self.core_v1.read_namespaced_pod_log(pod_name, namespace)
            print(logs.strip())
            return phase == "Succeeded"
        except ApiException as exc:
            print(f"Probe pod error: {exc}")
            return False
        finally:
            with contextlib.suppress(ApiException):
                self.core_v1.delete_namespaced_pod(pod_name, namespace, grace_period_seconds=0)

    def _wait_for_pod_completion(self, pod_name: str, namespace: str, timeout: int = 60) -> str:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            pod = self.core_v1.read_namespaced_pod(pod_name, namespace)
            if pod.status.phase in ("Succeeded", "Failed"):
                return pod.status.phase
            time.sleep(2)
        return "Pending"
