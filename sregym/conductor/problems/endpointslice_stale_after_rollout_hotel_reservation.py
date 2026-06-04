from sregym.conductor.oracles.endpointslice_stale_after_rollout_mitigation import (
    EndpointSliceStaleAfterRolloutMitigationOracle,
)
from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.problems.base import Problem
from sregym.generators.fault.inject_virtual import VirtualizationFaultInjector
from sregym.service.apps.hotel_reservation import HotelReservation
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected


class EndpointSliceStaleAfterRolloutHotelReservation(Problem):
    """
    Simulates stale EndpointSlice discovery state after a rollout.

    The Service selector remains correct, but EndpointSlice records still
    reference terminated pod IPs after a deployment update.
    """

    def __init__(self):
        self.app = HotelReservation()
        self.namespace = self.app.namespace
        self.faulty_service = "frontend"
        super().__init__(app=self.app, namespace=self.namespace)
        self.kubectl = KubeCtl()
        self.root_cause = self.build_structured_root_cause(
            component="endpointslice/controller",
            namespace=self.namespace,
            description=(
                "After a deployment rollout, EndpointSlice state is stale and contains references to terminated pod IPs. "
                "The Service may route traffic to outdated backend endpoints, causing intermittent request failures."
            ),
        )

        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)

        self.app.create_workload()
        self.mitigation_oracle = EndpointSliceStaleAfterRolloutMitigationOracle(problem=self)

    @mark_fault_injected
    def inject_fault(self):
        injector = VirtualizationFaultInjector(namespace=self.namespace)
        injector._inject(
            fault_type="stale_endpointslice_after_rollout",
            microservices=[self.faulty_service],
        )
        print(f"[FAULT INJECTED] stale EndpointSlice after rollout for service {self.faulty_service}")

    @mark_fault_injected
    def recover_fault(self):
        injector = VirtualizationFaultInjector(namespace=self.namespace)
        injector._recover(
            fault_type="stale_endpointslice_after_rollout",
            microservices=[self.faulty_service],
        )
        print(f"[FAULT RECOVERED] stale EndpointSlice after rollout for service {self.faulty_service}")
