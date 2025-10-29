from sregym.conductor.oracles.base import Oracle


class ServiceEndpointMitigationOracle(Oracle):
    """
    Oracle that verifies every Service has at least one ready endpoint.
    """

    importance = 1.0

    def evaluate(self) -> dict:
        print("== Service Endpoints Evaluation ==")

        kubectl = self.problem.kubectl
        namespace = self.problem.namespace

        # Always verify every Service in the namespace.
        services_to_check = [svc.metadata.name for svc in kubectl.list_services(namespace).items]

        for svc_name in services_to_check:
            try:
                ep = kubectl.core_v1_api.read_namespaced_endpoints(svc_name, namespace)
                has_ready = any(subset.addresses for subset in (ep.subsets or []))
                if not has_ready:
                    print(f"❌ Service {svc_name} has NO ready endpoints")
                    return {"success": False}
            except Exception as e:
                print(f"❌ Error retrieving endpoints for service {svc_name}: {e}")
                return {"success": False}

        print("[✅] All checked services have ready endpoints.")
        return {"success": True}
