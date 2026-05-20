from kubernetes import client
from kubernetes.client.rest import ApiException

from sregym.conductor.oracles.base import Oracle


class WrongPodSelectionMitigationOracle(Oracle):
    """Verify a Service has recovered from endpoint pollution."""

    importance = 1.0

    def evaluate(self) -> dict:
        print("== Wrong Pod Selection Mitigation Evaluation ==")

        kubectl = self.problem.kubectl
        namespace = self.problem.namespace
        service_name = self.problem.frontend_service
        expected_pod_label = self.problem.expected_endpoint_pod_label

        selected_pods = self._endpoint_pod_names(kubectl, namespace, service_name)
        if not selected_pods:
            print(f"Service {service_name} has no endpoint pods")
            return {"success": False}

        wrong_pods = []
        for pod_name in selected_pods:
            pod = kubectl.core_v1_api.read_namespaced_pod(pod_name, namespace)
            labels = pod.metadata.labels or {}
            if labels.get("io.kompose.service") != expected_pod_label:
                wrong_pods.append(pod_name)

        if wrong_pods:
            print(f"Service {service_name} still selects non-frontend endpoint pods: {wrong_pods}")
            return {"success": False}

        print(f"Service {service_name} endpoints are non-empty and all point to frontend pods.")
        return {"success": True}

    def _endpoint_pod_names(self, kubectl, namespace: str, service_name: str) -> set[str]:
        discovery = client.DiscoveryV1Api()
        try:
            endpoint_slices = discovery.list_namespaced_endpoint_slice(
                namespace=namespace,
                label_selector=f"kubernetes.io/service-name={service_name}",
            )
            pod_names = set()
            for endpoint_slice in endpoint_slices.items:
                for endpoint in endpoint_slice.endpoints or []:
                    if endpoint.target_ref and endpoint.target_ref.kind == "Pod":
                        ready = endpoint.conditions.ready if endpoint.conditions else None
                        if ready is not False:
                            pod_names.add(endpoint.target_ref.name)
            if pod_names:
                return pod_names
            print("EndpointSlice lookup returned no pod targetRefs, falling back to Endpoints API.")
        except ApiException as e:
            print(f"EndpointSlice lookup failed, falling back to Endpoints API: {e}")

        endpoints = kubectl.core_v1_api.read_namespaced_endpoints(service_name, namespace)
        pod_names = set()
        for subset in endpoints.subsets or []:
            for address in subset.addresses or []:
                if address.target_ref and address.target_ref.kind == "Pod":
                    pod_names.add(address.target_ref.name)
        return pod_names
