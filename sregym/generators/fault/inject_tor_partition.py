import json
import shlex
import subprocess
from typing import List, Tuple, Dict

from ray import nodes

from sregym.generators.fault.base import FaultInjector
from sregym.generators.noise.transient_issues.chaos_injector import ChaosInjector
from sregym.service.kubectl import KubeCtl


class ToRPartitionFaultInjector(FaultInjector):
    """
    Fault injector that uses ChaosMesh NetworkChaos to simulate a top-of-rack (ToR) router failure.
    """

    def __init__(self, testbed, namespace: str):
        super().__init__(testbed)
        self.namespace = namespace
        self.chaos_injector = ChaosInjector(namespace=namespace)
        self.kubectl = KubeCtl()
        self.experiment_name = f"tor-partition-{namespace}"

    def inject(self, target_node: str = None):
        namespace = self.namespace
        if target_node:
            selected_node = self._find_node_starting_with(target_node)
            if not selected_node:
                print(f"Node starting with '{target_node}' not found, selecting node with most pods")
                selected_node = self._find_node_with_most_pods(namespace)
        else:
            selected_node = self._find_node_with_most_pods(namespace)
        
        print(f"Selected target node: {selected_node}")
        
        tor_pods = self._get_pods_on_node(namespace, selected_node)
        if not tor_pods:
            raise RuntimeError(
                f"No running pods found on node '{selected_node}' in namespace '{namespace}'"
            )
        
        print(f"Found {len(tor_pods)} pods on node {selected_node}: {', '.join(tor_pods)}")

        other_pods = self._get_pods_not_on_node(namespace, selected_node)
        if not other_pods:
            raise RuntimeError(
                f"No pods found on other nodes in namespace '{namespace}' - need at least 2 nodes with pods for ToR partition"
            )

        print(
            f"Found {len(tor_pods)} pods on node {selected_node}, {len(other_pods)} pods on other nodes"
        )

        chaos_manifest = self._build_networkchaos_manifest(tor_pods, other_pods)
        self.chaos_injector.create_chaos_experiment(
            experiment_yaml=chaos_manifest,
            experiment_name=self.experiment_name,
        )
        print(f"ToR partition injected as NetworkChaos '{self.experiment_name}'")

    def recover(self, fault_type: str):
        namespace = self.namespace
        print(f"Recovering ToR partition: deleting '{self.experiment_name}'")
        try:
            self.chaos_injector.delete_chaos_experiment(self.experiment_name)
        except Exception as e:
            print(f"Error during recovery: {e}")
    


    """
    Helpers
    """    

    def _cleanup_if_exists(self):
        """
        Best-effort cleanup of any leftover Chaos Mesh experiment from
        a previous run (e.g., if the test was interrupted).

        We deliberately ignore errors here to keep injection robust.
        """
        try:
            self.chaos_injector.delete_chaos_experiment(self.experiment_name)
            print(f"Cleaned up leftover experiment {self.experiment_name}")
        except Exception as e:
            print(f"No existing experiment to clean up or cleanup failed non-fatally: {e}"
            )


    def _build_networkchaos_manifest(self, tor_pods: List[str], other_pods: List[str]) -> Dict:
        """
        Build a Chaos Mesh NetworkChaos manifest that partitions:
          - pods on the victim node (tor_pods)
          - from pods on all other nodes (other_pods)
        using pods-based selectors.
        """
        if not tor_pods or not other_pods:
            raise ValueError("tor_pods and other_pods must both be non-empty")

        # Strip "ns/" prefix for Chaos Mesh pod selector
        tor_names = [p.split("/", 1)[1] for p in tor_pods]
        other_names = [p.split("/", 1)[1] for p in other_pods]

        nodes = self._get_all_nodes()
        if len(nodes) < 2:
            raise RuntimeError("ToR partition requires at least 2 Kubernetes nodes")


        return {
            "apiVersion": "chaos-mesh.org/v1alpha1",
            "kind": "NetworkChaos",
            "metadata": {
                "name": self.experiment_name,
                "namespace": "chaos-mesh", 
            },
            "spec": {
                "action": "partition",
                "mode": "all",
                "selector": {
                    "pods": {
                        self.namespace: tor_names,
                    },
                },
                "direction": "both",
                "target": {
                    "mode": "all",
                    "selector": {
                        "pods": {
                            self.namespace: other_names,
                        },
                    },
                },
            },
        }

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

    def _get_pods_not_on_node(self, namespace: str, excluded_node: str) -> List[str]:
        """Get all Running pods in namespace that are NOT on excluded_node."""
        pods: List[str] = []
        cmd = f"kubectl -n {namespace} get pods -o json"
        out = self.kubectl.exec_command(cmd)
        if isinstance(out, tuple):
            out = out[0]

        try:
            data = json.loads(out or "{}")
            for item in data.get("items", []):
                phase = item.get("status", {}).get("phase")
                node_name = item.get("spec", {}).get("nodeName")
                if phase == "Running" and node_name and node_name != excluded_node:
                    pods.append(f"{namespace}/{item['metadata']['name']}")
        except Exception as e:
            print(f"[ToRPartitionFaultInjector] Error getting pods not on node {excluded_node}: {e}")

        return pods