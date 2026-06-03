from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.oracles.readiness_probe_missing_binary_mitigation import ReadinessProbeMissingBinaryMitigationOracle
from sregym.conductor.problems.base import Problem
from sregym.generators.fault.inject_virtual import VirtualizationFaultInjector
from sregym.service.apps.hotel_reservation import HotelReservation
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected


class ReadinessProbeMissingBinary(Problem):
    """Problem where the recommendation service has a readiness probe executing a non-existent binary,
    leaving the pod Running but unready (0/1 READY) because the binary is missing from the image.
    """

    def __init__(self, faulty_service: str = "recommendation"):
        self.app = HotelReservation()
        self.namespace = self.app.namespace
        super().__init__(app=self.app, namespace=self.namespace)

        self.kubectl = KubeCtl()
        self.faulty_service = faulty_service

        self.root_cause = self.build_structured_root_cause(
            component=self.faulty_service,
            namespace=self.namespace,
            description=(
                f"The deployment `{self.faulty_service}` has a misconfigured readiness probe that attempts to execute "
                f"a non-existent binary `/usr/local/bin/healthcheck`, causing the readiness probe to fail with command-not-found "
                f"errors. The pod remains in Running state but is never marked Ready, so the service excludes it from endpoints "
                f"and callers experience connection failures or timeouts."
            ),
        )

        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)
        self.app.create_workload()
        self.mitigation_oracle = ReadinessProbeMissingBinaryMitigationOracle(problem=self)

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection: Readiness Probe Missing Binary ==")
        self.injector = VirtualizationFaultInjector(namespace=self.namespace)
        self.injector._inject(
            fault_type="readiness_probe_missing_binary",
            microservices=[self.faulty_service],
        )
        print(f"Service: {self.faulty_service} | Namespace: {self.namespace}\n")

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery: Readiness Probe Missing Binary ==")
        self.injector._recover(
            fault_type="readiness_probe_missing_binary",
            microservices=[self.faulty_service],
        )
        print(f"Service: {self.faulty_service} | Namespace: {self.namespace}\n")
