from srearena.conductor.oracles.localization import LocalizationOracle
from srearena.conductor.oracles.mitigation import MitigationOracle
from srearena.conductor.problems.base import Problem
from srearena.generators.fault.inject_virtual import VirtualizationFaultInjector
from srearena.service.apps.social_network import SocialNetwork
from srearena.service.kubectl import KubeCtl
from srearena.utils.decorators import mark_fault_injected


class TaintNoToleration(Problem):
    required_config = ["faulty_service", "faulty_nodes"]
    
    def __init__(self):
        self.app = SocialNetwork()
        self.namespace = self.app.namespace
        self.kubectl = KubeCtl()
        self.config = {}

        super().__init__(app=self.app, namespace=self.namespace)

        self.localization_oracle = LocalizationOracle(problem=self, expected=[self.faulty_service])

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
        print(f"Injecting Fault to Service {self.config['faulty_service']} on Nodes {self.config['faulty_nodes']}")
        for node in self.config['faulty_nodes']:
            self.kubectl.exec_command(f"kubectl taint node {node} sre-fault=blocked:NoSchedule --overwrite")

        patch = """[{"op": "add", "path": "/spec/template/spec/tolerations",
                     "value": [{"key": "dummy-key", "operator": "Exists", "effect": "NoSchedule"}]}]"""
        self.kubectl.exec_command(
            f"kubectl patch deployment {self.config['faulty_service']} -n {self.namespace} --type='json' -p='{patch}'"
        )
        self.kubectl.exec_command(f"kubectl delete pod -l app={self.config['faulty_service']} -n {self.namespace}")

    @mark_fault_injected
    def recover_fault(self):
        print("Fault Recovery")
        # assuming recover_toleration_without_matching_taint can accept multiple services and a node list
        for node in self.faulty_nodes:
            self.injector.recover_toleration_without_matching_taint([self.faulty_service], node_name=node)


    def deploy_fault_config(self, config):
        self.config = config
        
    def init_config_iteration(self):
        # get all the deployments in the namespace
        deployments = self.kubectl.core_v1_api.list_deployment(namespace=self.namespace).items
        for deployment in deployments:
            if deployment.spec.template.spec.tolerations:
                self.candidate_deployments.append(deployment.metadata.name)
        
    def iterate_fault_config(self):
        self.config["faulty_nodes"] = self._pick_worker_nodes()
        self.config["faulty_service"] = self.candidate_deployments.pop(0)
        