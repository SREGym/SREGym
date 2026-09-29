import logging

from kubernetes import client

from sregym.conductor.oracles.base import Oracle
from sregym.conductor.oracles.failure import FailureClass


class IngressMisrouteMitigationOracle(Oracle):
    def __init__(self, problem):
        super().__init__(problem=problem)
        self.networking_v1 = client.NetworkingV1Api()
        self.logger = logging.getLogger(__name__)

    FAILURE_CLASSES = {
        # Deleting the path is an edit to the Ingress, not a symptom of one.
        "ingress_path_missing": FailureClass.AGENT_ERROR,
    }

    def evaluate(self) -> bool:
        results = {}
        try:
            ingress = self.networking_v1.read_namespaced_ingress(
                name=self.problem.ingress_name, namespace=self.problem.namespace
            )
            for rule in ingress.spec.rules:
                for path in rule.http.paths:
                    if path.path.startswith(self.problem.path):
                        if path.backend.service.name == self.problem.correct_service:
                            self.logger.info(
                                f"Ingress path '{self.problem.path}' correctly routed to '{self.problem.correct_service}'."
                            )
                            results["success"] = True
                            return results
                        else:
                            self.logger.info(
                                f"Ingress path '{self.problem.path}' still routed to '{path.backend.service.name}', mitigation incomplete."
                            )
                            # The misrouted path is the injected fault, read
                            # straight off the Ingress.
                            return self.fail(
                                "fault_still_present",
                                path=self.problem.path,
                                routed_to=path.backend.service.name,
                            )
            self.logger.error("Path not found in ingress, mitigation incomplete.")
            # The path is gone from the Ingress entirely rather than pointing
            # somewhere wrong: removal, not misroute.
            return self.fail("ingress_path_missing", path=self.problem.path)
        except client.exceptions.ApiException as e:
            self.logger.error(f"Error checking ingress configuration: {e}")
            return self.fail_from_exception(e)
