from sregym.conductor.oracles.alert_oracle import AlertOracle
from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.oracles.mitigation import MitigationOracle
from sregym.conductor.problems.base import Problem
from sregym.generators.fault.inject_virtual import VirtualizationFaultInjector
from sregym.service.apps.social_network import SocialNetwork
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected


class TaintNoToleration(Problem):
    def __init__(self):
        self.app = SocialNetwork()
        self.namespace = self.app.namespace
        self.kubectl = KubeCtl()
        super().__init__(app=self.app, namespace=self.namespace)

        # ── pick all nodes so the control-plane cannot be used as fallback ──
        self.faulty_nodes = self._pick_all_nodes()
        self.faulty_service = "user-service"
        self.root_cause = f"Worker nodes are tainted with sre-fault=blocked:NoSchedule, but the deployment `{self.faulty_service}` has a toleration for a different key (dummy-key), causing pods to be unschedulable and remain in Pending state."

        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)
        # TODO: support more precise diagnosis oracle: Nodes or DeploymentConfiguration

        self.app.create_workload()
        self.resolution_oracle = MitigationOracle(problem=self)
        self.mitigation_oracle = AlertOracle(problem=self)

        self.injector = VirtualizationFaultInjector(namespace=self.namespace)

    def _pick_all_nodes(self) -> list[str]:
        """Return the names of all nodes in the cluster."""
        nodes = self.kubectl.core_v1_api.list_node().items
        return [n.metadata.name for n in nodes]

    @mark_fault_injected
    def inject_fault(self):
        print(f"Injecting Fault to Service {self.faulty_service} on Nodes {self.faulty_nodes}")
        for node in self.faulty_nodes:
            self.kubectl.exec_command(f"kubectl taint node {node} sre-fault=blocked:NoSchedule --overwrite")

        patch = """[{"op": "add", "path": "/spec/template/spec/tolerations",
                     "value": [{"key": "dummy-key", "operator": "Exists", "effect": "NoSchedule"}]}]"""
        self.kubectl.exec_command(
            f"kubectl patch deployment {self.faulty_service} -n {self.namespace} --type='json' -p='{patch}'"
        )
        self.kubectl.exec_command(f"kubectl delete pod -l app={self.faulty_service} -n {self.namespace}")

    @mark_fault_injected
    def recover_fault(self):
        print("Fault Recovery")
        # assuming recover_toleration_without_matching_taint can accept multiple services and a node list
        for node in self.faulty_nodes:
            self.injector.recover_toleration_without_matching_taint([self.faulty_service], node_name=node)
