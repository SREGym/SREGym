from kubernetes.client.exceptions import ApiException

from sregym.conductor.oracles.alert_oracle import AlertOracle
from sregym.conductor.oracles.llm_as_a_judge import LLMAsAJudgeOracle
from sregym.conductor.problems.base import Problem
from sregym.generators.fault.inject_remote_os import RemoteOSFaultInjector
from sregym.service.apps.hotel_reservation import HotelReservation
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected


class NodeRoutingTableWipeout(Problem):
    def __init__(self):
        self.app = HotelReservation()
        super().__init__(app=self.app, namespace=self.app.namespace)
        self.kubectl = KubeCtl()
        self.injector = RemoteOSFaultInjector()
        self.victim_node = None

        self.root_cause = self.build_structured_root_cause(
            component="node/kernel-routing-table",
            namespace=self.namespace,
            description=(
                "Per-pod kernel routing entries are missing from one worker node's kernel routing table. "
                "Packets destined for those pods arrive at the node but have no route to their network "
                "interface and are dropped. Pods on the affected node cannot complete TCP connections "
                "in either direction; Kubernetes still reports them as Ready unless they crash trying "
                "to reach other services."
            ),
        )

        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)
        self.mitigation_oracle = AlertOracle(problem=self)
        self.app.create_workload()

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")
        self.victim_node = self._select_victim_node()
        print(f"Victim node: {self.victim_node}")
        self.injector.flush_pod_routes(self.victim_node)

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")
        if not self.victim_node:
            return
        pods = self.kubectl.core_v1_api.list_pod_for_all_namespaces(field_selector=f"spec.nodeName={self.victim_node}")
        affected_namespaces = {pod.metadata.namespace for pod in pods.items}
        for pod in pods.items:
            try:
                self.kubectl.core_v1_api.delete_namespaced_pod(
                    pod.metadata.name,
                    pod.metadata.namespace,
                    grace_period_seconds=0,
                )
            except ApiException as e:
                if e.status != 404:
                    raise
        print(f"Deleted all pods on {self.victim_node}")
        for ns in affected_namespaces:
            self.kubectl.wait_for_ready(ns)

    def _select_victim_node(self) -> str:
        pods = self.kubectl.list_pods(self.namespace)
        nodes_with_pods = {pod.spec.node_name for pod in pods.items if pod.spec.node_name}
        workers = set(self._select_worker_nodes())
        candidates = list(nodes_with_pods & workers)
        if not candidates:
            raise RuntimeError(f"No worker nodes have pods in {self.namespace}")
        return candidates[0]

    def _select_worker_nodes(self) -> list[str]:
        control_plane_labels = {"node-role.kubernetes.io/control-plane", "node-role.kubernetes.io/master"}
        return [
            node.metadata.name
            for node in self.kubectl.list_nodes().items
            if not control_plane_labels & set((node.metadata.labels or {}).keys())
        ]
