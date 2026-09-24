from sregym.conductor.oracles.cpu_throttling_mitigation import CpuThrottlingMitigationOracle
from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.problems.base import Problem
from sregym.generators.fault.inject_virtual import VirtualizationFaultInjector
from sregym.service.apps.hotel_reservation import HotelReservation
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected


class CpuThrottling(Problem):
    # Vary the stateless application tier so the target cannot be identified by
    # comparing it with a uniform 1-core baseline. Stateful dependencies are
    # deliberately excluded: resizing their pod templates breaks existing
    # database/cache connections and creates an unrelated outage.
    CPU_LIMIT_DECOYS = [
        "profile",
        "rate",
        "recommendation",
        "reservation",
        "search",
        "user",
        "frontend",
    ]

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
                "100ms scheduling window, delaying bursts and causing intermittent tail latency on search paths "
                "that depend on this service. Crucially, `kubectl top pods` shows CPU usage well below the limit — the "
                "throttling is silent at the kubectl level but visible in the container's cgroup stats "
                "(`cpu.stat`: high `nr_throttled`) and in the Prometheus `ContainerCPUThrottling` alert. "
                "The fix is to raise the CPU limit to a value that accommodates burst traffic, or remove it entirely."
            ),
        )
        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)
        # Calibration is deliberately fresh for every injection. Its sampling
        # floor is 65s (30s warmup + 5x7s windows); remote cgroup reads normally
        # make calibration take about 2-4 minutes, with bounded retries taking longer.
        # Verification repeats a settle/sample cycle when a service needs more headroom.
        self.app.create_workload(duration=180)
        self.mitigation_oracle = CpuThrottlingMitigationOracle(
            problem=self,
            faulty_service=self.faulty_service,
        )

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")
        injector = VirtualizationFaultInjector(namespace=self.namespace)
        injected = {}
        try:
            injected = injector.inject_cpu_throttle(
                microservices=[self.faulty_service],
                additional_services=self.CPU_LIMIT_DECOYS,
            )
            self._patched_services = list(injected)
            injected = injector.verify_injection(injected=injected, faulty_services=[self.faulty_service])
        except Exception:
            if injected:
                print("CPU-throttling verification failed; restoring original resources")
                injector.recover_cpu_throttle(microservices=list(injected))
            raise
        self.injected_cpu_limit = injected.get(self.faulty_service)
        self.mitigation_oracle.injected_cpu_limit = self.injected_cpu_limit
        print(f"Service: {self.faulty_service} | Namespace: {self.namespace}\n")

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")
        injector = VirtualizationFaultInjector(namespace=self.namespace)
        services = getattr(self, "_patched_services", [self.faulty_service])
        injector.recover_cpu_throttle(microservices=services)
        print(f"Service: {self.faulty_service} | Namespace: {self.namespace}\n")
