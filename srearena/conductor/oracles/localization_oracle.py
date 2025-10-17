from typing import Any

from kubernetes import client, config
from kubernetes.config.config_exception import ConfigException

from srearena.conductor.oracles.base import Oracle


class LocalizationOracle(Oracle):
    def __init__(self, problem: str, expected: list[str]):
        super().__init__(problem)
        self.expected = expected

    def get_resource_uid(self, resource_type: str, resource_name: str, namespace: str) -> str | None:
        """Return the UID of a live resource using the Kubernetes API."""
        try:
            try:
                config.load_incluster_config()
            except ConfigException:
                config.load_kube_config()
            if resource_type.lower() == "pod":
                api = client.CoreV1Api()
                obj = api.read_namespaced_pod(resource_name, namespace)
            elif resource_type.lower() == "service":
                api = client.CoreV1Api()
                obj = api.read_namespaced_service(resource_name, namespace)
            elif resource_type.lower() == "deployment":
                api = client.AppsV1Api()
                obj = api.read_namespaced_deployment(resource_name, namespace)
            elif resource_type.lower() == "statefulset":
                api = client.AppsV1Api()
                obj = api.read_namespaced_stateful_set(resource_name, namespace)
            else:
                raise ValueError(f"Unsupported resource type: {resource_type}")

            return obj.metadata.uid

        except client.exceptions.ApiException as e:
            return f"Error retrieving UID for {resource_type}/{resource_name} in {namespace}: {e.reason}"

    # def get_ground_truth(self, problem: str) -> dict[str, Any]:
    #     """Fetch ground truth UIDs from Kubernetes for a given problem."""
    #     PROBLEM_RESOURCE_MAP = {
    #         "operator_overload_replicas": [
    #             {"resource_type": "deployment", "resource_name": "basic-tidb", "namespace": "tidb-cluster"},
    #         ],
    #         "operator_invalid_affinity_toleration": [
    #             {"resource_type": "statefulset", "resource_name": "basic-tidb", "namespace": "tidb-cluster"},
    #         ],
    #         "operator_security_context_fault": [
    #             {"resource_type": "pod", "resource_name": "basic-tidb-0", "namespace": "tidb-cluster"},
    #         ],
    #         "operator_non_existent_storage": [
    #             {"resource_type": "statefulset", "resource_name": "basic-tikv", "namespace": "tidb-cluster"},
    #         ],

    #     }

    #     resources = PROBLEM_RESOURCE_MAP.get(problem)
    #     if not resources:
    #         raise ValueError(f"No ground truth mapping found for problem '{problem}'")

    #     results = {}
    #     for res in resources:
    #         uid = self.get_resource_uid(res["resource_type"], res["resource_name"], res["namespace"])
    #         results[f"{res['resource_type']}/{res['resource_name']}"] = uid

    #     return results

    def evaluate(self, solution) -> dict[str, Any]:
        """Compare Kubernetes ground truth UIDs with agent-provided solution (expected values)."""
        ground_truth = self.get_ground_truth(self.problem)
        comparison = {}
        problem_name = getattr(self.problem, "problem_id", str(self.problem))

        for key, truth_uid in ground_truth.items():
            expected_uid = (
                solution.get(key)
                if isinstance(solution, dict)
                else solution[0] if isinstance(solution, list) else str(solution)
            )

            comparison[key] = {
                "ground_truth_uid": truth_uid,
                "expected_uid": expected_uid,
                "match": expected_uid == truth_uid,
            }

        return {
            "problem": problem_name,
            "results": comparison,
            "matched": any(v["match"] for v in comparison.values()),
        }

    def get_ground_truth(self, problem: str) -> dict[str, Any]:
        """Fetch ground truth UIDs from Kubernetes dynamically.
        If the problem defines a faulty service, use its pods; otherwise, fallback to all pods in namespace.
        """
        try:
            try:
                config.load_incluster_config()
            except ConfigException:
                config.load_kube_config()
        except Exception as e:
            raise RuntimeError(f"Failed to load kube config: {e}")

        core_v1 = client.CoreV1Api()
        ns = getattr(self.problem, "namespace", "default")

        faulty_services = []
        if hasattr(self.problem, "faulty_service"):
            faulty_services = [self.problem.faulty_service]
        elif hasattr(self.problem, "faulty_services"):
            faulty_services = getattr(self.problem, "faulty_services", [])

        results = {}

        if faulty_services:
            for svc in faulty_services:
                pods = core_v1.list_namespaced_pod(namespace=ns, label_selector=f"app={svc}").items
                if not pods:
                    print(f"[WARN] No pods found for service '{svc}' in namespace '{ns}'")
                for pod in pods:
                    results[f"pod/{pod.metadata.name}"] = pod.metadata.uid

        if not results:
            pods = core_v1.list_namespaced_pod(namespace=ns).items
            for pod in pods:
                results[f"pod/{pod.metadata.name}"] = pod.metadata.uid
            print(f"[INFO] Using all pods in namespace '{ns}' as fallback ground truth")

        if not results:
            raise ValueError(f"No pods found for problem '{problem}' in namespace '{ns}'")

        return results
