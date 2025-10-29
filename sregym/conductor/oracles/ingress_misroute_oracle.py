import logging
from kubernetes import client
from sregym.conductor.oracles.base import Oracle

class IngressMisrouteMitigationOracle(Oracle):
    def __init__(self, problem):
        super().__init__(problem=problem)
        self.networking_v1 = client.NetworkingV1Api()
        self.logger = logging.getLogger(__name__)

    def evaluate(self) -> bool:
        results = {}
        try:
            ingress = self.networking_v1.read_namespaced_ingress(name=self.problem.ingress_name, namespace=self.problem.namespace)
            for rule in ingress.spec.rules:
                for path in rule.http.paths:
                    if path.path == self.problem.path:
                        if path.backend.service.name == self.problem.correct_service:
                            self.logger.info(f"Ingress path '{self.problem.path}' correctly routed to '{self.problem.correct_service}'.")
                            results["success"] = True
                            return results
                        else:
                            self.logger.info(f"Ingress path '{self.problem.path}' still routed to '{path.backend.service.name}', mitigation incomplete.")
                            results["success"] = False
                            return results
            self.logger.error("Path not found in ingress, mitigation incomplete.")
            results["success"] = False
        except client.exceptions.ApiException as e:
            self.logger.error(f"Error checking ingress configuration: {e}")
            results["success"] = False
        return results
