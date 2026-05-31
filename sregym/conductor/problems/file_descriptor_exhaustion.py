import json
import subprocess
from sregym.service.apps.hotel_reservation import HotelReservation
from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.oracles.mitigation import MitigationOracle
from sregym.generators.fault.inject_virtual import VirtualizationFaultInjector
from sregym.conductor.problems.base import Problem
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected

class FileDescriptorExhaustion(Problem):
    def __init__(self):
        self.app = HotelReservation()
        self.namespace = self.app.namespace
        self.faulty_service = "recommendation"
        self.forced_ulimit = 20

        super().__init__(app=self.app, namespace=self.namespace)
        self.kubectl = KubeCtl()
        self.root_cause = self.build_structured_root_cause(
            component=f"deployment/{self.faulty_service}",
            namespace=f"{self.namespace}",
            description=(
                f"The {self.faulty_service} deployment is encountering file descriptor exhaustion. "
                f"The current limit (ulimit -n {self.forced_ulimit}) is insufficient for the deployment, "
                f"causing the 'Too many open files' error."
            ),
        )
        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)

        self.app.create_workload()
        self.mitigation_oracle = MitigationOracle(problem=self)

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")
        injector = VirtualizationFaultInjector(namespace = self.namespace)
        injector.inject_fd_exhaustion(
            microservices=[self.faulty_service],
            entrypoint_cmd=f"{self.faulty_service}",
            limit = 10
        )
        print(f"Service: {self.faulty_service} | Namespace: {self.namespace}\n")

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")
        injector = VirtualizationFaultInjector(namespace=self.namespace)
        injector.recover_fd_exhaustion(
            microservices=[self.faulty_service],
            entrypoint_cmd=f"{self.faulty_service}"
        )
        print(f"Service: {self.faulty_service} | Namespace: {self.namespace}\n")