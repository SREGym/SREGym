"""Inject resource-contention / brownout faults via Kubernetes resource limits."""

from sregym.generators.fault.base import FaultInjector
from sregym.service.kubectl import KubeCtl


class ResourceContentionFaultInjector(FaultInjector):
    def __init__(self, namespace: str):
        super().__init__(namespace)
        self.namespace = namespace
        self.kubectl = KubeCtl()
        self._original_cpu_limit: dict[str, str] = {}

    def _jsonpath(self, service: str, path: str) -> str:
        out = self.kubectl.exec_command(
            f'kubectl get deployment {service} -n {self.namespace} -o jsonpath="{{{path}}}"'
        )
        return (out or "").strip().strip("'\"")

    def _first_container_name(self, service: str) -> str:
        return self._jsonpath(service, ".spec.template.spec.containers[0].name")

    def _read_cpu_limit(self, service: str) -> str:
        return self._jsonpath(service, ".spec.template.spec.containers[0].resources.limits.cpu")

    def inject_cpu_throttling(self, microservices: list[str], limit_millicores: int):
        """Add a tight CPU limit in place so the container is CFS-throttled under load."""
        for service in microservices:
            container = self._first_container_name(service)
            self._original_cpu_limit[service] = self._read_cpu_limit(service)
            self.kubectl.exec_command(
                f"kubectl set resources deployment/{service} -n {self.namespace} "
                f"-c {container} --limits=cpu={limit_millicores}m"
            )
            self.kubectl.exec_command(
                f"kubectl rollout status deployment/{service} -n {self.namespace} --timeout=300s"
            )
            print(f"Injected cpu limit {limit_millicores}m on deployment/{service} ({container}) in {self.namespace}")

    def recover_cpu_throttling(self, microservices: list[str]):
        """Restore the original CPU limit, or remove the injected one."""
        for service in microservices:
            container = self._first_container_name(service)
            original = self._original_cpu_limit.get(service, "")
            if original:
                self.kubectl.exec_command(
                    f"kubectl set resources deployment/{service} -n {self.namespace} "
                    f"-c {container} --limits=cpu={original}"
                )
            else:
                self.kubectl.exec_command(
                    f"kubectl patch deployment {service} -n {self.namespace} --type=json "
                    "-p='[{\"op\":\"remove\",\"path\":\"/spec/template/spec/containers/0/resources/limits/cpu\"}]'"
                )
            self.kubectl.exec_command(
                f"kubectl rollout status deployment/{service} -n {self.namespace} --timeout=300s"
            )
            print(f"Recovered deployment/{service} in {self.namespace}")