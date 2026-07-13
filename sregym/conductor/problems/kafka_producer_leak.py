from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.problems.base import Problem
from sregym.generators.fault.inject_app import ApplicationFaultInjector
from sregym.service.apps.astronomy_shop import AstronomyShop
from sregym.conductor.oracles.kafka_producer_leak_mitigation import KafkaProducerLeakOracle
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected


class KafkaProducerLeak(Problem):
    """
    Problem that injects a sidecar container that floods the Kafka broker with large concurrent messages, exhausting broker heap memory
    """

    def __init__(self):
        self.app_name = "astronomy_shop"
        self.faulty_service = "checkout"

        self.app = AstronomyShop()

        self.namespace = self.app.namespace
        super().__init__(app=self.app, namespace=self.namespace)

        self.kubectl = KubeCtl()
        self.root_cause = self.build_structured_root_cause(
            component=f"deployment/{self.faulty_service}",
            namespace=f"{self.namespace}",
            description=f"The {self.faulty_service} deployment has a sidecar container that continuously sends large messages to the Kafka broker concurrently, causing the broker to exhaust its heap memory through network receive buffer allocation pressure"
        )
        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)

        self.mitigation_oracle = KafkaProducerLeakOracle(problem=self)

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")
        injector = ApplicationFaultInjector(namespace=self.namespace)
        injector.inject_kafka_producer_leak(self.faulty_service)
        print(f"Service: {self.faulty_service} | Namespace: {self.namespace}\n")

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")
        injector = ApplicationFaultInjector(namespace=self.namespace)
        injector.recover_kafka_producer_leak(self.faulty_service)
        print(f"Service: {self.faulty_service} | Namespace: {self.namespace}\n")
