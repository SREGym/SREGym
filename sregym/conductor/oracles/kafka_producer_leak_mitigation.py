import datetime
import time

from sregym.conductor.oracles.failure import FailureClass
from sregym.conductor.oracles.mitigation import MitigationOracle
from sregym.service.kafka_health import KafkaHealthCheck, broker_memory_failure, broker_pods
from sregym.service.kubectl import ApiException, KubeCtl


class KafkaProducerLeakOracle(MitigationOracle):
    FAILURE_CLASSES = {
        "kafka_still_restarting": FailureClass.AMBIGUOUS,
        "kafka_not_serving": FailureClass.AMBIGUOUS,
    }

    def evaluate(self) -> dict:
        try:
            return self._evaluate()
        except Exception as exc:
            return self.fail_from_exception(exc)

    def _evaluate(self) -> dict:
        self.rollout_time = 300

        results = super().evaluate()

        if results["success"]:
            kubectl: KubeCtl = self.problem.kubectl

            try:
                checkout_deployment = kubectl.get_deployment(self.problem.faulty_service, self.problem.namespace)
                kafka_deployment = kubectl.get_deployment("kafka", self.problem.namespace)
            except ApiException as exc:
                # Silently swallowed before: an unreadable Deployment scored
                # identically to a heap limit the agent never restored.
                return self.fail_from_exception(exc)

            if not checkout_deployment.spec.replicas or not kafka_deployment.spec.replicas:
                print("❌ checkout or kafka Deployment is scaled to zero")
                return self.fail(
                    "required_deployment_scaled_to_zero",
                    checkout_replicas=checkout_deployment.spec.replicas,
                    kafka_replicas=kafka_deployment.spec.replicas,
                )

            for c in kafka_deployment.spec.template.spec.containers:
                if "kafka" in c.name:
                    heap = next((e.value for e in c.env or [] if e.name == "KAFKA_HEAP_OPTS"), None)
                    if heap != self.problem.heap_limit:
                        return self.fail(
                            "fault_still_present",
                            setting="KAFKA_HEAP_OPTS",
                            value=heap,
                            expected=self.problem.heap_limit,
                        )

                    memory_limit = c.resources.limits.get("memory") if c.resources and c.resources.limits else None
                    if memory_limit != self.problem.memory_limit:
                        print(f"❌ kafka memory limit is '{memory_limit}', expected '{self.problem.memory_limit}'")
                        return self.fail(
                            "fault_still_present",
                            setting="memory_limit",
                            value=memory_limit,
                            expected=self.problem.memory_limit,
                        )

                    break

            with KafkaHealthCheck(kubectl, self.problem.namespace) as probe:
                started = datetime.datetime.now(datetime.UTC)
                # Kafka's container can be Ready before its protocol listener
                # starts. Give a valid manual restart time to finish, then
                # require the entire stability window below.
                ready_deadline = time.monotonic() + 60
                while True:
                    if broker_memory_failure(kubectl, self.problem.namespace, started):
                        return self.fail("fault_still_present", detail="Kafka has a new memory failure")
                    if probe.check():
                        break
                    if time.monotonic() >= ready_deadline:
                        return self.fail("kafka_not_serving")
                    time.sleep(2)
                before = self._broker_restarts()
                deadline = time.monotonic() + 120
                next_probe = 0
                while True:
                    after = self._broker_restarts()
                    if not before or before != after:
                        return self.fail("kafka_still_restarting", before=before, after=after)
                    if broker_memory_failure(kubectl, self.problem.namespace, started):
                        return self.fail("fault_still_present", detail="Kafka has a new memory failure")
                    now = time.monotonic()
                    if now >= next_probe or now >= deadline:
                        if not probe.check():
                            return self.fail("kafka_not_serving")
                        next_probe = time.monotonic() + 20
                    if now >= deadline:
                        break
                    time.sleep(min(5, max(0, deadline - time.monotonic())))

        return results

    def _broker_restarts(self):
        return {
            str(pod.metadata.uid): status.restart_count
            for pod in broker_pods(self.problem.kubectl, self.problem.namespace)
            for status in pod.status.container_statuses or []
            if status.name == "kafka"
        }
