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
            elif resource_type.lower() == "persistentvolumeclaim":
                api = client.CoreV1Api()
                obj = api.read_namespaced_persistent_volume_claim(resource_name, namespace)
            elif resource_type.lower() == "persistentvolume":
                api = client.CoreV1Api()
                obj = api.read_persistent_volume(resource_name)
            else:
                raise ValueError(f"Unsupported resource type: {resource_type}")

            return obj.metadata.uid

        except client.exceptions.ApiException as e:
            return f"Error retrieving UID for {resource_type}/{resource_name} in {namespace}: {e.reason}"

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
            "success": any(v["match"] for v in comparison.values()),
        }

    def get_ground_truth(self, problem: str) -> dict[str, Any]:
        """Fetch ground truth UIDs from Kubernetes dynamically.
        - Includes both Service and Pod UIDs for faulty services.
        - Supports Kompose (`io.kompose.service`) and standard (`app`) labels.
        - Falls back to all pods if no match is found.
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

        faulty_services: list[str] = []
        if hasattr(self.problem, "faulty_service"):
            raw = getattr(self.problem, "faulty_service")
            if isinstance(raw, list):
                faulty_services.extend(raw)
            elif isinstance(raw, str):
                faulty_services.append(raw.strip("[]'\" "))
        elif hasattr(self.problem, "faulty_services"):
            raw = getattr(self.problem, "faulty_services", [])
            if isinstance(raw, list):
                faulty_services.extend(raw)
            elif isinstance(raw, str):
                faulty_services.append(raw.strip("[]'\" "))

        faulty_services = [s for s in faulty_services if s]
        faulty_services = list(dict.fromkeys(faulty_services))

        print(f"[DEBUG] Namespace: {ns}")
        print(f"[DEBUG] Faulty services: {faulty_services}")

        results: dict[str, Any] = {}

        if faulty_services:
            for svc in faulty_services:
                try:
                    try:
                        service_obj = core_v1.read_namespaced_service(svc, namespace=ns)
                        svc_uid = service_obj.metadata.uid
                        results[f"service/{svc}"] = svc_uid
                        print(f"[INFO] Found service '{svc}' (UID: {svc_uid})")
                    except client.exceptions.ApiException as e:
                        print(f"[WARN] No service named '{svc}' in '{ns}' ({e.reason})")

                    pods = core_v1.list_namespaced_pod(namespace=ns, label_selector=f"io.kompose.service={svc}").items

                    if not pods:
                        pods = core_v1.list_namespaced_pod(namespace=ns, label_selector=f"app={svc}").items

                    if not pods:
                        print(f"[WARN] No pods found for '{svc}' — using all pods in '{ns}'.")
                        pods = core_v1.list_namespaced_pod(namespace=ns).items

                    for pod in pods:
                        results[f"pod/{pod.metadata.name}"] = pod.metadata.uid
                        print(f"[INFO] Found pod '{pod.metadata.name}' (UID: {pod.metadata.uid}) under service '{svc}'")

                except client.exceptions.ApiException as e:
                    print(f"[ERROR] Failed to query '{svc}' in '{ns}': {e.reason}")
        else:
            pods = core_v1.list_namespaced_pod(namespace=ns).items
            for pod in pods:
                results[f"pod/{pod.metadata.name}"] = pod.metadata.uid
            print(f"[INFO] Using all pods in namespace '{ns}' as fallback ground truth")

        if not results:
            raise ValueError(f"No pods or services found for problem '{problem}' in namespace '{ns}'")

        return results
