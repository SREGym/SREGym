import logging

from kubernetes import client

from srearena.conductor.oracles.base import Oracle


class NetworkPolicyMitigationOracle(Oracle):
    def __init__(self, problem, policy_name=None):
        super().__init__(problem=problem)
        self.networking_v1 = client.NetworkingV1Api()
        self.policy_name = policy_name or f"deny-all-{problem.faulty_service}"
        self.logger = logging.getLogger(__name__)

    def evaluate(self) -> bool:
        results = {}
        try:
            self.networking_v1.read_namespaced_network_policy(name=self.policy_name, namespace=self.problem.namespace)
            # Policy still exists, mitigation incomplete
            self.logger.info(f"NetworkPolicy '{self.policy_name}' still present, mitigation not complete.")
            results["success"] = False
        except client.exceptions.ApiException as e:
            if e.status == 404:
                self.logger.info(f"NetworkPolicy '{self.policy_name}' not found, mitigation successful.")
                results["success"] = True
            else:
                self.logger.error(f"Error checking NetworkPolicy: {e}")
                results["success"] = False
        return results
