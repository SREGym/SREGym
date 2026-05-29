"""Problem: HPA cannot compute CPU utilization due to missing effective CPU requests.

The fault models a Kubernetes HorizontalPodAutoscaler control-loop failure. The
HPA targets Hotel Reservation's frontend deployment and uses CPU utilization,
but the frontend pods do not have an effective CPU request. The pods may remain
Running/Ready, while the HPA reports ``<unknown>/60%`` and
``FailedGetResourceMetric``.

For this target, the injector removes both ``resources.requests.cpu`` and
``resources.limits.cpu``. Removing only the request is not reliable because
Kubernetes can use a CPU limit as the effective request when the request is
omitted.
"""

from sregym.conductor.oracles.hpa_control_plane_mitigation import HPAControlPlaneMitigationOracle
from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.problems.base import Problem
from sregym.generators.fault.inject_virtual import VirtualizationFaultInjector
from sregym.service.apps.hotel_reservation import HotelReservation
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected


class HPAMissingEffectiveCPURequest(Problem):
    """Inject a broken CPU-utilization HPA on Hotel Reservation frontend."""

    HPA_NAME = "frontend-capacity"

    def __init__(self):
        self.app = HotelReservation()
        self.namespace = self.app.namespace
        self.faulty_service = "frontend"

        super().__init__(app=self.app, namespace=self.namespace)

        self.kubectl = KubeCtl()

        self.root_cause = self.build_structured_root_cause(
            component=f"HorizontalPodAutoscaler/{self.HPA_NAME}",
            namespace=self.namespace,
            description=(
                f"The HorizontalPodAutoscaler `{self.HPA_NAME}` targets "
                f"`Deployment/{self.faulty_service}` in namespace `{self.namespace}` and uses CPU "
                "utilization, but the targeted frontend pods do not have an effective CPU request. "
                "CPU utilization is calculated relative to CPU requests, so the HPA cannot compute "
                "the metric and reports `<unknown>/60%`, `ScalingActive=False`, and "
                "`FailedGetResourceMetric` with a message like `missing request for cpu`. "
                "The frontend pods may still be Running/Ready; the fault is the broken autoscaling "
                "control loop. A valid mitigation restores a computable CPU metric, typically by "
                "restoring CPU requests. Manually scaling the deployment without fixing the HPA is "
                "not sufficient."
            ),
        )
        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)

        self.app.create_workload()

        self.mitigation_oracle = HPAControlPlaneMitigationOracle(
            problem=self,
            deployment_name=self.faulty_service,
            hpa_name=self.HPA_NAME,
        )

    @mark_fault_injected
    def inject_fault(self):
        """Inject the HPA missing effective CPU request fault."""
        print("== Fault Injection ==")
        injector = VirtualizationFaultInjector(namespace=self.namespace)
        injector.inject_hpa_missing_effective_cpu_request(
            microservices=[self.faulty_service],
            hpa_name=self.HPA_NAME,
        )
        print(f"Service: {self.faulty_service} | Namespace: {self.namespace}\n")

    @mark_fault_injected
    def recover_fault(self):
        """Restore frontend resources while keeping the HPA healthy for validation."""
        print("== Fault Recovery ==")
        injector = VirtualizationFaultInjector(namespace=self.namespace)
        injector.recover_hpa_missing_effective_cpu_request(
            microservices=[self.faulty_service],
            hpa_name=self.HPA_NAME,
        )
        print(f"Service: {self.faulty_service} | Namespace: {self.namespace}\n")
