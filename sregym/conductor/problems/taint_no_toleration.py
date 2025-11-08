from sregym.conductor.oracles.deployment_itself_localization_oracle import DeploymentItselfLocalizationOracle
from sregym.conductor.oracles.localization import LocalizationOracle
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

        # ── pick all real worker nodes dynamically ───────────────────────
        self.faulty_nodes = self._pick_worker_nodes()
        self.faulty_service = "user-service"

        super().__init__(app=self.app, namespace=self.namespace)

        self.localization_oracle = DeploymentItselfLocalizationOracle(
            problem=self, namespace=self.namespace, expected_deployment_names=[self.faulty_service]
        )
        # TODO: support more precise localization oracle: Nodes or DeploymentConfiguration

        self.app.create_workload()
        self.mitigation_oracle = MitigationOracle(problem=self)

        self.injector = VirtualizationFaultInjector(namespace=self.namespace)

    def _pick_worker_nodes(self) -> list[str]:
        """Return the names of all nodes that are *not* control-plane."""
        nodes = self.kubectl.core_v1_api.list_node().items
        worker_names = []
        for n in nodes:
            labels = n.metadata.labels or {}
            if "node-role.kubernetes.io/control-plane" not in labels:
                worker_names.append(n.metadata.name)
        if not worker_names:
            # fallback to first node if somehow all are control-plane
            return [nodes[0].metadata.name]
        return worker_names

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
