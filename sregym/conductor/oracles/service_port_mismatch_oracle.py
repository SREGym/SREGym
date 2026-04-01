import json

from sregym.conductor.oracles.base import Oracle


class ServicePortMismatchMitigationOracle(Oracle):
    importance = 1.0

    def _normalize_port(self, value):
        if value is None:
            return None
        if hasattr(value, "value"):
            value = value.value
        try:
            return int(value)
        except (TypeError, ValueError):
            return str(value)

    def _get_geo_runtime_port(self) -> int | None:
        namespace = self.problem.namespace
        pod_name = self.problem.kubectl.get_pod_name(namespace, self.problem.pod_label_selector)
        raw = self.problem.kubectl.exec_command(
            f"kubectl exec -n {namespace} {pod_name} -- cat {self.problem.config_path}"
        )
        try:
            config = json.loads(raw)
        except json.JSONDecodeError:
            return None

        geo_port = config.get("GeoPort")
        try:
            return int(geo_port)
        except (TypeError, ValueError):
            return None

    def _service_is_safely_fixed(self, service_port: int | str | None, target_port: int | str | None) -> bool:
        return (
            service_port == self.problem.original_service_port
            and target_port == self.problem.original_target_port
        )

    def _service_is_fixed_via_backend_rollout(
        self,
        service_port: int | str | None,
        target_port: int | str | None,
        geo_runtime_port: int | None,
    ) -> bool:
        return (
            service_port == self.problem.original_service_port
            and target_port == self.problem.wrong_target_port
            and geo_runtime_port == self.problem.wrong_target_port
        )

    def evaluate(self) -> dict:
        print("== Service Port Mitigation Evaluation ==")

        kubectl = self.problem.kubectl
        namespace = self.problem.namespace
        faulty_service = self.problem.faulty_service
        results = {}
        service_port = None
        target_port = None
        geo_runtime_port = None

        endpoints_ready = False
        try:
            service = kubectl.get_service(faulty_service, namespace)
            service_port = self._normalize_port(service.spec.ports[0].port if service.spec.ports else None)
            target_port = self._normalize_port(service.spec.ports[0].target_port if service.spec.ports else None)

            endpoints = kubectl.core_v1_api.read_namespaced_endpoints(faulty_service, namespace)
            endpoints_ready = any(subset.addresses for subset in (endpoints.subsets or []))
            if not endpoints_ready:
                print(f"Service {faulty_service} has no ready endpoints")
        except Exception as exc:
            print(f"Error retrieving endpoints for service {faulty_service}: {exc}")

        try:
            geo_runtime_port = self._get_geo_runtime_port()
        except Exception as exc:
            print(f"Error retrieving runtime geo port: {exc}")

        pods_running = True
        for pod in kubectl.list_pods(namespace).items:
            if pod.status.phase != "Running":
                print(f"Pod {pod.metadata.name} is in phase {pod.status.phase}")
                pods_running = False
                break

            container_statuses = pod.status.container_statuses or []
            if not container_statuses or not all(container_status.ready for container_status in container_statuses):
                print(f"Pod {pod.metadata.name} is not fully ready")
                pods_running = False
                break

        safe_fix = self._service_is_safely_fixed(service_port, target_port)
        disruptive_fix = self._service_is_fixed_via_backend_rollout(service_port, target_port, geo_runtime_port)

        if not (safe_fix or disruptive_fix):
            print(
                "Accepted end states are either "
                f"Service port/targetPort {self.problem.original_service_port}->{self.problem.original_target_port} "
                "or Service port fixed to the original front-door port while the geo backend has been rolled to "
                f"listen on {self.problem.wrong_target_port}."
            )

        results["success"] = endpoints_ready and pods_running and (safe_fix or disruptive_fix)
        results["fix_mode"] = "safe_service_patch" if safe_fix else "backend_rollout" if disruptive_fix else "invalid"
        results["service_port"] = service_port
        results["target_port"] = target_port
        results["geo_runtime_port"] = geo_runtime_port

        print(f"Mitigation Result: {'Pass ✅' if results['success'] else 'Fail ❌'}")

        return results
