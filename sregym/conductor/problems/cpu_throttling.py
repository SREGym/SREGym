from sregym.conductor.oracles.cpu_throttling_mitigation import CpuThrottlingMitigationOracle
from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.problems.base import Problem
from sregym.generators.fault.inject_virtual import VirtualizationFaultInjector
from sregym.service.apps.hotel_reservation import HotelReservation
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected


class CpuThrottling(Problem):
    def __init__(self, faulty_service: str = "geo"):
        self.app_name = "hotel_reservation"
        self.faulty_service = faulty_service
        self.app = HotelReservation()
        self.namespace = self.app.namespace
        super().__init__(app=self.app, namespace=self.namespace)

        self.kubectl = KubeCtl()
        self.root_cause = self.build_structured_root_cause(
            component=f"deployment/{self.faulty_service}",
            namespace=self.namespace,
            description=(
                f"The `{self.faulty_service}` deployment has a CPU limit set too low for its normal operation. "
                "The Linux CFS scheduler throttles the container whenever it exceeds its quota within a "
                "100ms scheduling window, causing severe latency increases on search paths that depend on "
                "this service. Crucially, `kubectl top pods` shows CPU usage well below the limit — the "
                "throttling is silent at the kubectl level but visible in the container's cgroup stats "
                "(`cpu.stat`: high `nr_throttled`) and in the Prometheus `ContainerCPUThrottling` alert. "
                "The fix is to raise the CPU limit to a value that accommodates burst traffic, or remove "
                "it entirely. Many kubernetes good practices guides for critical applications recommend "
                "removing it "
            ),
        )
        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)
        self.app.create_workload()
        self.mitigation_oracle = CpuThrottlingMitigationOracle(
            problem=self,
            faulty_service=self.faulty_service,
        )

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")
        injector = VirtualizationFaultInjector(namespace=self.namespace)
        injector.inject_cpu_throttle(microservices=[self.faulty_service])
        print(f"Service: {self.faulty_service} | Namespace: {self.namespace}\n")

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")
        injector = VirtualizationFaultInjector(namespace=self.namespace)
        injector.recover_cpu_throttle(microservices=[self.faulty_service])
        print(f"Service: {self.faulty_service} | Namespace: {self.namespace}\n")
