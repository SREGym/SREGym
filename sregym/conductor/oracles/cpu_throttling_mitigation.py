import time

from sregym.conductor.oracles.mitigation import MitigationOracle

_ROLLOUT_SETTLE_SECONDS = 60
_ROLLOUT_POLL_INTERVAL = 5


def _parse_cpu_millicores(cpu_str: str) -> int | None:
    if not cpu_str:
        return None
    cpu_str = cpu_str.strip()
    if cpu_str.endswith("m"):
        return int(cpu_str[:-1])
    return int(float(cpu_str) * 1000)


class CpuThrottlingMitigationOracle(MitigationOracle):
    importance = 1.0

    def __init__(self, problem, faulty_service: str):
        super().__init__(problem)
        self.faulty_service = faulty_service
        self.injected_cpu_limit: str | None = None
        self.rollout_timeout_seconds = _ROLLOUT_SETTLE_SECONDS
        self.poll_interval_seconds = _ROLLOUT_POLL_INTERVAL

    @staticmethod
    def _rollout_complete(deployment) -> bool:
        desired = deployment.spec.replicas if deployment.spec.replicas is not None else 1
        status = deployment.status
        return (
            desired > 0
            and (status.observed_generation or 0) >= (deployment.metadata.generation or 0)
            and (status.updated_replicas or 0) == desired
            and (status.ready_replicas or 0) == desired
            and (status.available_replicas or 0) == desired
            and (status.unavailable_replicas or 0) == 0
        )

    def _wait_for_required_deployments(self) -> dict | None:
        if not self.replica_count:
            print("Pre-injection Deployment baseline was not captured")
            return None

        deadline = time.monotonic() + self.rollout_timeout_seconds
        while True:
            try:
                deployments = self.problem.kubectl.list_deployments(self.problem.namespace)
            except Exception as exc:
                print(f"Could not list required Deployments: {exc}")
                return None

            current = {deployment.metadata.name: deployment for deployment in deployments.items}
            missing = sorted(set(self.replica_count) - set(current))
            if missing:
                print(f"Required Deployment(s) missing: {', '.join(missing)}")
                return None

            unsettled = []
            for name, baseline_replicas in self.replica_count.items():
                deployment = current[name]
                desired = deployment.spec.replicas if deployment.spec.replicas is not None else 1
                minimum = baseline_replicas if baseline_replicas is not None else 1
                if desired < max(1, minimum):
                    print(
                        f"Required Deployment '{name}' requests {desired} replicas; "
                        f"healthy baseline requested {minimum}"
                    )
                    return None
                if not self._rollout_complete(deployment):
                    unsettled.append(name)

            if not unsettled:
                return current
            if time.monotonic() >= deadline:
                print("Required Deployment(s) are not fully updated, Ready, and Available: " + ", ".join(unsettled))
                return None
            time.sleep(self.poll_interval_seconds)

    def evaluate(self) -> dict:
        print("== Mitigation Evaluation (CpuThrottling) ==")

        deployments = self._wait_for_required_deployments()
        if deployments is None:
            return {"success": False}

        try:
            deployment = deployments[self.faulty_service]
        except KeyError:
            print(f"Deployment '{self.faulty_service}' not found")
            return {"success": False}

        injected_mc = _parse_cpu_millicores(self.injected_cpu_limit) if self.injected_cpu_limit else None
        for container in deployment.spec.template.spec.containers:
            limits = (container.resources and container.resources.limits) or {}
            cpu_limit_str = limits.get("cpu") if isinstance(limits, dict) else getattr(limits, "cpu", None)
            if cpu_limit_str is None:
                continue
            if injected_mc is not None:
                cpu_mc = _parse_cpu_millicores(str(cpu_limit_str))
                if cpu_mc is not None and cpu_mc <= injected_mc * 2:
                    print(
                        f"Container '{container.name}' still has a throttling CPU limit: {cpu_limit_str} "
                        f"(<= {injected_mc * 2}m)"
                    )
                    return {"success": False}

        print(
            f"Deployment '{self.faulty_service}' CPU limit is fixed; "
            "all required Deployments are fully Ready and Available"
        )
        return {"success": True}
