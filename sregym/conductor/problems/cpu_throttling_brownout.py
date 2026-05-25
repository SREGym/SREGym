"""CPU-throttling brownout problem."""

from sregym.conductor.oracles.compound import CompoundedOracle
from sregym.conductor.oracles.cpu_throttling_mitigation import CpuThrottlingRatioOracle
from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.oracles.mitigation import MitigationOracle
from sregym.conductor.problems.base import Problem
from sregym.generators.fault.inject_resource_contention import ResourceContentionFaultInjector
from sregym.service.apps.astronomy_shop import AstronomyShop
from sregym.service.apps.hotel_reservation import HotelReservation
from sregym.service.apps.social_network import SocialNetwork
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected

_APPS = {
    "hotel_reservation": HotelReservation,
    "social_network": SocialNetwork,
    "astronomy_shop": AstronomyShop,
}


class CpuThrottlingBrownout(Problem):
    """Brownout fault: a too-tight CPU limit causes CFS throttling under load."""

    def __init__(
        self,
        app_name: str = "hotel_reservation",
        faulty_service: str = "frontend",
        limit_millicores: int = 50,
    ):
        if app_name not in _APPS:
            raise ValueError(f"Unsupported app name: {app_name}")

        self.app_name = app_name
        self.faulty_service = faulty_service
        self.limit_millicores = limit_millicores

        self.app = _APPS[app_name]()
        self.namespace = self.app.namespace
        super().__init__(app=self.app, namespace=self.namespace)

        self.kubectl = KubeCtl()
        self.injector = ResourceContentionFaultInjector(namespace=self.namespace)

        self.root_cause = self.build_structured_root_cause(
            component=f"deployment/{self.faulty_service}",
            namespace=self.namespace,
            description=(
                f"The {self.faulty_service} deployment has resources.limits.cpu set to "
                f"{self.limit_millicores}m, far below the CPU the service needs under "
                "request load. Kubernetes enforces this limit as a Linux CFS bandwidth "
                "quota: once the container exhausts its quota within a 100ms scheduling "
                "period it is throttled (paused) until the next period. The bursty "
                "request handler is repeatedly stalled, inflating request and tail "
                "latency. The pods stay Running and Ready and average CPU utilization "
                "appears low, so the throttling is only visible in the "
                "container_cpu_cfs_throttled_periods_total metric. The fix is to raise "
                "or remove the CPU limit on the deployment."
            ),
        )

        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)
        self.mitigation_oracle = CompoundedOracle(
            self,
            CpuThrottlingRatioOracle(problem=self, faulty_service=self.faulty_service),
            MitigationOracle(problem=self),
        )

        self.app.create_workload()

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")
        self.injector.inject_cpu_throttling(
            microservices=[self.faulty_service],
            limit_millicores=self.limit_millicores,
        )
        print(f"Service: {self.faulty_service} | Namespace: {self.namespace}\n")

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")
        self.injector.recover_cpu_throttling(microservices=[self.faulty_service])
        print(f"Service: {self.faulty_service} | Namespace: {self.namespace}\n")