"""Problem: Kafka poison-pill head-of-line (HOL) block."""

from sregym.conductor.oracles.data_plane_progress import DataPlaneProgressOracle
from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.problems.base import Problem
from sregym.generators.fault.inject_kafka import KafkaFaultInjector
from sregym.service.apps.astronomy_shop import AstronomyShop
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected


class KafkaPoisonPillHOLBlock(Problem):
    TOPIC = "orders-fulfillment"
    CONSUMER_GROUP = "orders-validator"
    CONSUMER_DEPLOYMENT = "orders-validator"

    def __init__(self):
        self.app = AstronomyShop()
        super().__init__(app=self.app, namespace=self.app.namespace)

        self.kubectl = KubeCtl()
        self.injector = KafkaFaultInjector(namespace=self.namespace)
        self.faulty_service = self.CONSUMER_DEPLOYMENT
        self.poison_offset = KafkaFaultInjector.SEED_RECORD_COUNT

        self.root_cause = self.build_structured_root_cause(
            component=f"kafka topic/{self.TOPIC}",
            namespace=self.namespace,
            description=(
                f"An unprocessable ('poison-pill') record was published to the `{self.TOPIC}` "
                f"Kafka topic. The `{self.CONSUMER_GROUP}` consumer group cannot deserialize "
                "this record, so it never commits the offset and halts at that position "
                "(head-of-line blocking). All consumer pods stay Running and Ready, but the "
                "consumer-group committed offset is frozen and partition lag grows without "
                "bound — order-fulfillment processing has silently stopped. Restarting or "
                "rescaling the consumer does not help: a restarted pod re-reads the same "
                "uncommitted offset and stalls again, because the fault state lives in the "
                "Kafka log, outside the Kubernetes control plane. Mitigation requires "
                "advancing the consumer group past the poison record (skip-to-offset / "
                "dead-letter-queue style) without skipping the valid records queued behind it."
            ),
        )

        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)
        self.mitigation_oracle = DataPlaneProgressOracle(
            problem=self,
            consumer_group=self.CONSUMER_GROUP,
            topic=self.TOPIC,
            consumer_deployment=self.CONSUMER_DEPLOYMENT,
            archiver_deployment=KafkaFaultInjector.ARCHIVER_DEPLOYMENT,
        )

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection: Kafka poison-pill HOL block ==")
        self.poison_offset = self.injector.inject()
        print(
            f"Poison record at offset {self.poison_offset} of topic '{self.TOPIC}'; "
            f"consumer group '{self.CONSUMER_GROUP}' will stall there."
        )

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery: Kafka poison-pill HOL block ==")
        self.injector.recover()