from sregym.conductor.oracles.image_pull_backoff_mitigation import ImagePullBackoffMitigationOracle
from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.problems.base import Problem
from sregym.generators.fault.inject_virtual import VirtualizationFaultInjector
from sregym.service.apps.hotel_reservation import HotelReservation
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected

_FAKE_REGISTRY = "fake-private-registry.sregym.internal"


class ImagePullWrongSecret(Problem):
    """Problem that injects incorrect registry credentials causing ImagePullBackOff.

    This simulates a real-world scenario where a deployment is updated to pull
    from a private container registry but the imagePullSecret contains invalid
    credentials (or the registry is unreachable), causing the pod to fail image
    pull with ErrImagePull / ImagePullBackOff.
    """

    def __init__(self, app_name: str = "hotel_reservation", faulty_service: str = "profile"):
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
                f"The {self.faulty_service} deployment was configured to pull its container image "
                f"from a private registry ({_FAKE_REGISTRY}) using an imagePullSecret with invalid "
                "credentials. Kubernetes cannot authenticate to the registry, causing the pod to "
                "enter ErrImagePull and then ImagePullBackOff status."
            ),
        )
        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)

        self.app.create_workload()
        self.mitigation_oracle = ImagePullBackoffMitigationOracle(
            problem=self,
            faulty_service=self.faulty_service,
            invalid_tag=_FAKE_REGISTRY,
        )

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")
        injector = VirtualizationFaultInjector(namespace=self.namespace)
        injector.inject_wrong_pull_secret(microservices=[self.faulty_service])
        print(f"Service: {self.faulty_service} | Namespace: {self.namespace}\n")

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")
        injector = VirtualizationFaultInjector(namespace=self.namespace)
        injector.recover_wrong_pull_secret(microservices=[self.faulty_service])
        print(f"Service: {self.faulty_service} | Namespace: {self.namespace}\n")
