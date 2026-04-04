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
        self.root_cause = self.build_structured_root_cause(
            component=self.faulty_service,
            namespace=self.namespace,
            description=(
                f"Cluster nodes are tainted with `sre-fault=blocked:NoSchedule`, but deployment `{self.faulty_service}` "
                "only has a non-matching toleration (`dummy-key`), so new pods cannot be scheduled onto any valid node. "
                "Pods remain in Pending with scheduler taint/toleration mismatch events instead of becoming Ready. "
                "Users observe sustained request failures or degraded responses because replacement capacity never starts."
            ),
        )

        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)
        # TODO: support more precise diagnosis oracle: Nodes or DeploymentConfiguration

        self.app.create_workload()
        self.mitigation_oracle = MitigationOracle(problem=self)

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
        # Step 1: Remove taints from all nodes first
        for node in self.faulty_nodes:
            self.kubectl.exec_command(f"kubectl taint node {node} sre-fault=blocked:NoSchedule-")
            print(f"Removed taint from node {node}")

        # Step 2: Delete any Pending pods cluster-wide so system components
        # (e.g. OpenEBS) that couldn't schedule during the fault can recover
        self.kubectl.exec_command("kubectl delete pods --field-selector=status.phase=Pending --all-namespaces")

        # Step 3: Restart the faulty service and wait for app namespace stability
        for svc in [self.faulty_service]:
            self.kubectl.exec_command(f"kubectl rollout restart deployment {svc} -n {self.namespace}")
        self.kubectl.wait_for_stable(self.namespace)
