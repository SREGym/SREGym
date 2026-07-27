from sregym.conductor.oracles.mitigation import MitigationOracle
from sregym.service.kubectl import KubeCtl, ApiException
import time

class KafkaProducerLeakOracle(MitigationOracle):
    def evaluate(self) -> dict:
        self.rollout_time = 300

        results = super().evaluate()

        if results["success"]:
            kubectl: KubeCtl = self.problem.kubectl

            try:
                checkout_deployment = kubectl.get_deployment(self.problem.faulty_service, self.problem.namespace)
                kafka_deployment = kubectl.get_deployment("kafka", self.problem.namespace)
            except ApiException:
                results["success"] = False
                return results
            
            if not checkout_deployment.spec.replicas or not kafka_deployment.spec.replicas:
                results["success"] = False
                return results

            for c in kafka_deployment.spec.template.spec.containers:
                if "kafka" in c.name:
                    for e in c.env:
                        if e.name == "KAFKA_HEAP_OPTS":
                            if e.value != self.problem.heap_limit:
                                results["success"] = False
                                return results
                            
                            break
                            
                    if (c.resources.limits.get("memory") if c.resources and c.resources.limits else None) != self.problem.memory_limit:
                        results["success"] = False
                        return results
                    
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
                    results["success"] = False
                    return results

                time.sleep(5)

        return results