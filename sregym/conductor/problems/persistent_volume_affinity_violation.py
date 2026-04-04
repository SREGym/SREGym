from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.oracles.mitigation import MitigationOracle
from sregym.conductor.problems.base import Problem
from sregym.generators.fault.inject_virtual import VirtualizationFaultInjector
from sregym.service.apps.app_registry import AppRegistry
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected


class PersistentVolumeAffinityViolation(Problem):
    def __init__(self, app_name: str = "Social Network", faulty_service: str = "user-service"):
        self.apps = AppRegistry()
        self.app = self.apps.get_app_instance(app_name)
        super().__init__(app=self.app, namespace=self.app.namespace)
        self.kubectl = KubeCtl()
        self.namespace = self.app.namespace
        self.faulty_service = faulty_service
        self.root_cause = self.build_structured_root_cause(
            component=self.faulty_service,
            namespace=self.namespace,
            description=(
                f"Deployment `{self.faulty_service}` requests storage from a PersistentVolume with node affinity on one "
                "node set, while its pod `nodeSelector` targets a different node set, creating an unsatisfiable placement. "
                "The scheduler reports volume node affinity conflicts and pods remain Pending rather than attaching storage. "
                "Users experience service unavailability or severe degradation because stateful pods never become Running."
            ),
        )

        # === Attach evaluation oracles ===
        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)

        self.mitigation_oracle = MitigationOracle(problem=self)

        self.app.create_workload()

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")
        print("Injecting persistent volume affinity violation...")

        injector = VirtualizationFaultInjector(namespace=self.namespace)
        injector._inject(
            fault_type="persistent_volume_affinity_violation",
            microservices=[self.faulty_service],
        )

        print(f"Expected effect: {self.faulty_service} pod should be stuck in Pending state")
        print(f"Service: {self.faulty_service} | Namespace: {self.namespace}\n")

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")
        injector = VirtualizationFaultInjector(namespace=self.namespace)
        injector._recover(
            fault_type="persistent_volume_affinity_violation",
            microservices=[self.faulty_service],
        )

        print(f"Service: {self.faulty_service} | Namespace: {self.namespace}\n")
