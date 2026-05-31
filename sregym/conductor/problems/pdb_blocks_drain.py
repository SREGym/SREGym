from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.oracles.pdb_blocks_drain_mitigation import PDBBlocksDrainMitigationOracle
from sregym.conductor.problems.base import Problem
from sregym.generators.fault.inject_virtual import VirtualizationFaultInjector
from sregym.service.apps.astronomy_shop import AstronomyShop
from sregym.service.apps.hotel_reservation import HotelReservation
from sregym.service.apps.social_network import SocialNetwork
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected


class PDBBlocksDrain(Problem):
    def __init__(self, app_name: str = "hotel_reservation", faulty_service: str = "frontend"):
        self.app_name = app_name

        self.faulty_service = faulty_service

        if app_name == "hotel_reservation":
            self.app = HotelReservation()

        elif app_name == "social_network":
            self.app = SocialNetwork()

        elif app_name == "astronomy_shop":
            self.app = AstronomyShop()

        else:
            raise ValueError(f"Unsupported app name: {app_name}")

        self.namespace = self.app.namespace

        super().__init__(app=self.app, namespace=self.namespace)

        self.kubectl = KubeCtl()

        self.root_cause = self.build_structured_root_cause(
            component=f"deployment/{self.faulty_service}",
            namespace=self.namespace,
            description=(
                "A PodDisruptionBudget protecting the deployment is configured with minAvailable "
                "equal to the replica count, so the budget permits zero voluntary disruptions. "
                "When the node hosting the pod is cordoned and drained for maintenance, the drain "
                "cannot evict the protected pod without violating the budget, so the node is stuck "
                "in SchedulingDisabled and the drain never completes. The application keeps serving, "
                "but voluntary maintenance operations on the node deadlock."
            ),
        )

        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)

        self.app.create_workload()

        self.mitigation_oracle = PDBBlocksDrainMitigationOracle(problem=self)

    @mark_fault_injected
    def inject_fault(self):
        print("Fault Injection")

        injector = VirtualizationFaultInjector(namespace=self.namespace)

        injector._inject(
            fault_type="pdb_blocks_node_drain",
            microservices=[self.faulty_service],
        )

        print(f"Service: {self.faulty_service} | Namespace: {self.namespace}\n")

    @mark_fault_injected
    def recover_fault(self):
        print("Fault Recovery")

        injector = VirtualizationFaultInjector(namespace=self.namespace)

        injector._recover(
            fault_type="pdb_blocks_node_drain",
            microservices=[self.faulty_service],
        )

        print(f"Service: {self.faulty_service} | Namespace: {self.namespace}\n")
