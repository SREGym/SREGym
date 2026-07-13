from sregym.conductor.oracles.mitigation import MitigationOracle

class KafkaProducerLeakOracle(MitigationOracle):
    def evaluate(self) -> dict:
        results = super().evaluate()

        if results["success"]:
            deployment = self.problem.kubectl.get_deployment(self.problem.faulty_service, self.problem.namespace)
            for c in deployment.spec.template.spec.containers:
                if c.name == "order-creator":
                    results["success"] = False
                    break

        return results