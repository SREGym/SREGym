import json

from sregym.conductor.oracles.base import Oracle


class FrontendGeoPortRolloutMitigationOracle(Oracle):
    importance = 1.0

    def _get_frontend_runtime_config(self) -> dict | None:
        pod_name = self.problem.kubectl.get_pod_name(self.problem.namespace, self.problem.pod_label_selector)
        raw = self.problem.kubectl.exec_command(
            f"kubectl exec -n {self.problem.namespace} {pod_name} -- cat {self.problem.config_path}"
        )
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None

    def _get_frontend_pod_uid(self) -> str | None:
        pod_name = self.problem.kubectl.get_pod_name(self.problem.namespace, self.problem.pod_label_selector)
        pod = self.problem.kubectl.core_v1_api.read_namespaced_pod(pod_name, self.problem.namespace)
        return getattr(pod.metadata, "uid", None)

    def evaluate(self) -> dict:
        print("== Frontend GeoPort Rollout Evaluation ==")

        kubectl = self.problem.kubectl
        namespace = self.problem.namespace
        results = {}

        config_ok = False
        runtime_config_ok = False
        frontend_restarted = False
        current_pod_uid = None
        config_geo_port = None
        runtime_geo_port = None

        try:
            configmap = kubectl.core_v1_api.read_namespaced_config_map(self.problem.configmap_name, namespace)
            config_json = json.loads((configmap.data or {}).get("service-config.json", "{}"))
            config_geo_port = config_json.get("GeoPort")
            config_ok = str(config_geo_port) == str(self.problem.expected_geo_port)
        except Exception as exc:
            print(f"Error reading frontend configmap {self.problem.configmap_name}: {exc}")

        try:
            runtime_config = self._get_frontend_runtime_config()
            runtime_geo_port = None if runtime_config is None else runtime_config.get("GeoPort")
            runtime_config_ok = str(runtime_geo_port) == str(self.problem.expected_geo_port)
            current_pod_uid = self._get_frontend_pod_uid()
            frontend_restarted = bool(current_pod_uid and current_pod_uid != self.problem.faulty_frontend_pod_uid)
        except Exception as exc:
            print(f"Error reading frontend runtime config: {exc}")

        frontend_ready = True
        for pod in kubectl.list_pods(namespace).items:
            if pod.metadata.name.startswith("frontend-") or pod.metadata.name.startswith("hotel-reserv-frontend"):
                if pod.status.phase != "Running":
                    print(f"Frontend pod {pod.metadata.name} is in phase {pod.status.phase}")
                    frontend_ready = False
                    break
                container_statuses = pod.status.container_statuses or []
                if not container_statuses or not all(cs.ready for cs in container_statuses):
                    print(f"Frontend pod {pod.metadata.name} is not fully ready")
                    frontend_ready = False
                    break

        results["success"] = config_ok and runtime_config_ok and frontend_restarted and frontend_ready
        results["config_geo_port"] = config_geo_port
        results["runtime_geo_port"] = runtime_geo_port
        results["frontend_restarted"] = frontend_restarted
        results["current_frontend_pod_uid"] = current_pod_uid
        results["expected_geo_port"] = self.problem.expected_geo_port

        print(f"Mitigation Result: {'Pass ✅' if results['success'] else 'Fail ❌'}")
        return results
