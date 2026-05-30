"""Hotel Reservation Service protocol mismatch fault.

This problem simulates a realistic Kubernetes Service misconfiguration where a
TCP microservice is accidentally exposed as UDP. Pods and endpoints can look
healthy, but TCP clients cannot use the Service correctly.
"""

from sregym.conductor.oracles.base import Oracle
from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.oracles.mitigation import MitigationOracle
from sregym.conductor.problems.base import Problem
from sregym.service.apps.hotel_reservation import HotelReservation
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected


class ServiceProtocolMismatchMitigationOracle(Oracle):
    """Mitigation succeeds only when the Service protocol is restored to TCP."""

    importance = 1.0

    def evaluate(self) -> dict:
        print("== Mitigation Evaluation ==")

        kubectl = self.problem.kubectl
        namespace = self.problem.namespace
        service_name = self.problem.faulty_service

        results = {}

        service = kubectl.get_service(service_name, namespace)
        ports = service.spec.ports or []

        if not ports:
            print(f"❌ Service {service_name} has no ports")
            results["success"] = False
            return results

        protocol = ports[0].protocol

        if protocol != "TCP":
            print(f"❌ Service {service_name} protocol is still {protocol}, expected TCP")
            results["success"] = False
            results["protocol"] = protocol
            return results

        print(f"✅ Service {service_name} protocol restored to TCP")

        # Also make sure the application pods are still healthy.
        pod_oracle = MitigationOracle(problem=self.problem)
        return pod_oracle.evaluate()


class ServiceProtocolMismatchHotelReservation(Problem):
    """Inject a wrong Service protocol into the Hotel Reservation recommendation Service."""

    def __init__(self):
        self.app = HotelReservation()
        self.namespace = self.app.namespace
        self.faulty_service = "recommendation"
        self.correct_protocol = "TCP"
        self.wrong_protocol = "UDP"

        super().__init__(app=self.app, namespace=self.namespace)

        self.kubectl = KubeCtl()
        self.root_cause = self.build_structured_root_cause(
            component=f"service/{self.faulty_service}",
            namespace=self.namespace,
            description=(
                f"The Kubernetes Service `{self.faulty_service}` is misconfigured with protocol "
                f"`{self.wrong_protocol}` even though the recommendation application speaks "
                f"`{self.correct_protocol}`. The recommendation pods and endpoints can remain healthy, "
                "but TCP callers cannot reliably reach the service through the Kubernetes Service virtual IP. "
                "This causes recommendation-dependent requests to fail or time out while normal pod health checks "
                "may still look green. The concrete fix is to restore the Service port protocol back to TCP."
            ),
        )

        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)

        self.app.create_workload()
        self.mitigation_oracle = ServiceProtocolMismatchMitigationOracle(problem=self)

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")

        patch = f'[{{"op":"replace","path":"/spec/ports/0/protocol","value":"{self.wrong_protocol}"}}]'
        result = self.kubectl.exec_command(
            f"kubectl patch service {self.faulty_service} -n {self.namespace} --type=json -p '{patch}'"
        )

        print(f"Patch result for {self.faulty_service}: {result}")
        print(
            f"Injected Service protocol mismatch for service: {self.faulty_service}\n"
            f"Protocol: {self.wrong_protocol} | Namespace: {self.namespace}\n"
        )

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")

        patch = f'[{{"op":"replace","path":"/spec/ports/0/protocol","value":"{self.correct_protocol}"}}]'
        result = self.kubectl.exec_command(
            f"kubectl patch service {self.faulty_service} -n {self.namespace} --type=json -p '{patch}'"
        )

        print(f"Patch result for {self.faulty_service}: {result}")
        print(
            f"Recovered Service protocol for service: {self.faulty_service}\n"
            f"Protocol: {self.correct_protocol} | Namespace: {self.namespace}\n"
        )
