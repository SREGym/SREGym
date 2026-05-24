from sregym.conductor.oracles.image_pull_backoff_mitigation import ImagePullBackoffMitigationOracle
from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.problems.base import Problem
from sregym.generators.fault.inject_virtual import VirtualizationFaultInjector
from sregym.service.apps.hotel_reservation import HotelReservation
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected


class ImagePullBackoff(Problem):
    """Problem that injects an invalid container image tag, causing ImagePullBackOff.

    This simulates a real-world scenario where a deployment is updated with a
    non-existent image tag (e.g., after a bad push, a typo, or a deleted registry tag),
    causing pods to fail to start with ErrImagePull / ImagePullBackOff status.
    """

    def __init__(self, app_name: str = "hotel_reservation", faulty_service: str = "search"):
        self.app_name = app_name
        self.faulty_service = faulty_service
        self.app = HotelReservation()
        self.namespace = self.app.namespace
        super().__init__(app=self.app, namespace=self.namespace)

        self.kubectl = KubeCtl()
        self.root_cause = self.build_structured_root_cause(
            component=f"deployment/{self.faulty_service}",
            namespace=self.namespace,
            description=(
                f"The {self.faulty_service} deployment was updated with a non-existent container image tag. "
                "Kubernetes cannot pull the image from the registry, causing the pod to enter "
                "ErrImagePull and then ImagePullBackOff status, making the service unavailable."
            ),
        )
        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)

        self.app.create_workload()
        self.mitigation_oracle = ImagePullBackoffMitigationOracle(
            problem=self,
            faulty_service=self.faulty_service,
        )

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")
        injector = VirtualizationFaultInjector(namespace=self.namespace)
        injector.inject_image_pull_backoff(microservices=[self.faulty_service])
        print(f"Service: {self.faulty_service} | Namespace: {self.namespace}\n")

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")
        injector = VirtualizationFaultInjector(namespace=self.namespace)
        injector.recover_image_pull_backoff(microservices=[self.faulty_service])
        print(f"Service: {self.faulty_service} | Namespace: {self.namespace}\n")
