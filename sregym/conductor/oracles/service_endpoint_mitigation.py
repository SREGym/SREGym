from sregym.conductor.oracles.base import Oracle


class ServiceEndpointMitigationOracle(Oracle):
    """Verify that the affected Service has ready endpoints for its Deployment."""

    importance = 1.0

    @staticmethod
    def _pod_matches_selector(pod, selector: dict[str, str]) -> bool:
        labels = pod.metadata.labels or {}
        return all(labels.get(key) == value for key, value in selector.items())

    def evaluate(self) -> dict:
        print("== Service Endpoints Evaluation ==")

        kubectl = self.problem.kubectl
        namespace = self.problem.namespace
        service_name = self.problem.faulty_service

        try:
            deployment = kubectl.get_deployment(service_name, namespace)
            deployment_selector = deployment.spec.selector.match_labels or {}
            if not deployment_selector:
                print(f"❌ Deployment {service_name} has no matchLabels selector")
                return {"success": False}

            expected_pods = {
                pod.metadata.name
                for pod in kubectl.list_pods(namespace).items
                if self._pod_matches_selector(pod, deployment_selector)
            }
            if not expected_pods:
                print(f"❌ Deployment {service_name} has no matching pods")
                return {"success": False}

            endpoints = kubectl.core_v1_api.read_namespaced_endpoints(service_name, namespace)
            ready_addresses = [address for subset in (endpoints.subsets or []) for address in (subset.addresses or [])]
            ready_pods = {
                address.target_ref.name
                for address in ready_addresses
                if address.target_ref is not None and address.target_ref.kind == "Pod"
            }

            if not ready_pods:
                print(f"❌ Service {service_name} has no ready pod endpoints")
                return {"success": False}

            unexpected_pods = ready_pods - expected_pods
            if unexpected_pods:
                print(f"❌ Service {service_name} selects unexpected pods: {', '.join(sorted(unexpected_pods))}")
                return {"success": False}
        except Exception as e:
            print(f"❌ Error retrieving endpoints for service {service_name}: {e}")
            return {"success": False}

        print(f"[✅] Service {service_name} has ready endpoints for its intended Deployment.")
        return {"success": True}
