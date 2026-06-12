import time

from sregym.conductor.oracles.base import Oracle

_ROLLOUT_SETTLE_SECONDS = 60
_ROLLOUT_POLL_INTERVAL = 5


def _parse_cpu_millicores(cpu_str: str) -> int | None:
    if not cpu_str:
        return None
    cpu_str = cpu_str.strip()
    if cpu_str.endswith("m"):
        return int(cpu_str[:-1])
    return int(float(cpu_str) * 1000)


class CpuThrottlingMitigationOracle(Oracle):
    importance = 1.0

    def __init__(self, problem, faulty_service: str):
        super().__init__(problem)
        self.faulty_service = faulty_service
        self.injected_cpu_limit: str | None = None

    def _wait_for_rollouts(self, kubectl, namespace):
        deadline = time.monotonic() + _ROLLOUT_SETTLE_SECONDS
        while time.monotonic() < deadline:
            deployments = kubectl.list_deployments(namespace)
            all_settled = all(
                (dep.status.updated_replicas or 0) >= (dep.spec.replicas or 1)
                and (dep.status.ready_replicas or 0) >= (dep.spec.replicas or 1)
                and (dep.status.unavailable_replicas or 0) == 0
                for dep in deployments.items
            )
            if all_settled:
                return
            time.sleep(_ROLLOUT_POLL_INTERVAL)
        print("Timed out waiting for rollouts to settle; evaluating current state")

    def evaluate(self) -> dict:
        print("== Mitigation Evaluation (CpuThrottling) ==")

        kubectl = self.problem.kubectl
        namespace = self.problem.namespace

        try:
            deployment = kubectl.get_deployment(self.faulty_service, namespace)
        except Exception:
            print(f"Deployment '{self.faulty_service}' not found — deleting it is not a valid fix")
            return {"success": False}

        desired = deployment.spec.replicas or 0
        if desired < 1:
            print(f"Deployment '{self.faulty_service}' scaled to {desired} — scaling to 0 is not a valid fix")
            return {"success": False}

        containers = deployment.spec.template.spec.containers
        for container in containers:
            limits = (container.resources and container.resources.limits) or {}
            cpu_limit_str = limits.get("cpu") if isinstance(limits, dict) else getattr(limits, "cpu", None)
            if cpu_limit_str is None:
                print(f"Container '{container.name}' has no CPU limit, removing it is NOT a valid fix")
                return {"success": False}
            injected_mc = _parse_cpu_millicores(self.injected_cpu_limit) if self.injected_cpu_limit else None
            if injected_mc is not None:
                cpu_mc = _parse_cpu_millicores(str(cpu_limit_str))
                if cpu_mc is not None and cpu_mc <= injected_mc * 2:
                    print(
                        f"Container '{container.name}' still has a throttling CPU limit: {cpu_limit_str} "
                        f"(<= {injected_mc * 2}m)"
                    )
                    return {"success": False}
        print(f"Deployment '{self.faulty_service}' CPU limit is fixed")

        self._wait_for_rollouts(kubectl, namespace)
        pod_list = kubectl.list_pods(namespace)

        if not pod_list.items:
            print("No pods found in namespace")
            return {"success": False}

        for pod in pod_list.items:
            if pod.status.phase != "Running":
                print(f"Pod {pod.metadata.name} is in phase: {pod.status.phase}")
                return {"success": False}
            for cs in pod.status.container_statuses or []:
                if cs.state.waiting and cs.state.waiting.reason:
                    print(f"Container {cs.name} is waiting: {cs.state.waiting.reason}")
                    return {"success": False}
                if not cs.ready:
                    print(f"Container {cs.name} is not ready")
                    return {"success": False}

        print("All pods are Running and ready")
        return {"success": True}
