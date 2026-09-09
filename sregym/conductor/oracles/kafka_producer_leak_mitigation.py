import time

from sregym.conductor.oracles.failure import FailureClass
from sregym.conductor.oracles.mitigation import MitigationOracle
from sregym.service.kubectl import ApiException, KubeCtl


class KafkaProducerLeakOracle(MitigationOracle):
    FAILURE_CLASSES = {
        "kafka_still_restarting": FailureClass.AMBIGUOUS,
    }

    def evaluate(self) -> dict:
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
                    for e in c.env:
                        if e.name == "KAFKA_HEAP_OPTS":
                            if e.value != self.problem.heap_limit:
                                print(f"❌ KAFKA_HEAP_OPTS is '{e.value}', expected '{self.problem.heap_limit}'")
                                # Compared against the value the injector set,
                                # so this is the fault observed directly.
                                return self.fail(
                                    "fault_still_present",
                                    setting="KAFKA_HEAP_OPTS",
                                    value=e.value,
                                    expected=self.problem.heap_limit,
                                )

                            break

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

            pods = kubectl.list_pods(self.problem.namespace)
            rcnt_1 = None
            for p in pods.items:
                if "kafka" in p.metadata.name:
                    for c in p.status.container_statuses:
                        if "kafka" in c.name:
                            rcnt_1 = c.restart_count
                            break
                    break

            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                pods = kubectl.list_pods(self.problem.namespace)
                rcnt_2 = None
                for p in pods.items:
                    if "kafka" in p.metadata.name:
                        for c in p.status.container_statuses:
                            if "kafka" in c.name:
                                rcnt_2 = c.restart_count
                                break
                        break

                if rcnt_1 is None or rcnt_2 is None or rcnt_2 > rcnt_1:
                    print(f"❌ kafka container restarted during the watch window ({rcnt_1} -> {rcnt_2})")
                    # The broker is still OOMing even with the limits restored,
                    # so the configuration is right but the outcome is not.
                    return self.fail("kafka_still_restarting", before=rcnt_1, after=rcnt_2)

                time.sleep(5)

        return results
