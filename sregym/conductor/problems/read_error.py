from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.problems.base import Problem
from sregym.generators.fault.inject_hw import HWFaultInjector
from sregym.paths import TARGET_MICROSERVICES
from sregym.service.apps.hotel_reservation import HotelReservation
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected


class ReadError(Problem):
    """
    Problem: inject syscall-level EIO (-5) failures into `read()` for all pods on a target node.
    """

    def __init__(self, target_node: str = None):
        self.app = HotelReservation()
        self.kubectl = KubeCtl()
        self.namespace = self.app.namespace
        self.injector = HWFaultInjector()
        self.target_node = target_node

        # (Optional) pick a request mix payload
        self.app.payload_script = (
            TARGET_MICROSERVICES / "hotelReservation/wrk2/scripts/hotel-reservation/mixed-workload_type_1.lua"
        )

        super().__init__(app=self.app, namespace=self.app.namespace)
        self.root_cause = f"System call-level EIO (-5) failures are injected into `read()` operations for all pods on a target node, causing I/O errors and service failures."

        self.app.create_workload()

    def requires_khaos(self) -> bool:
        """This problem requires Khaos for eBPF-based fault injection."""
        return True

    @mark_fault_injected
    def inject_fault(self):
        print(f"== Fault Injection: read_error ==")
        self.target_node = self.injector.inject_node(self.namespace, "read_error", self.target_node)
        print(f"[debug] target_node: {self.target_node}")
        # Setup diagnosis oracle here since we now have the target node
        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)
        print(f"Injected read_error into pods on node {self.target_node}\n")

    @mark_fault_injected
    def recover_fault(self):
        print(f"== Fault Recovery: read_error on node {self.target_node} ==")
        if self.target_node:
            self.injector.recover_node(self.namespace, "read_error", self.target_node)
        else:
            print("[warn] No target node recorded; attempting best-effort recovery.")
        print("Recovery request sent.\n")
