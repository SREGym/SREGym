"""Mitigation oracle for NightlyRebalanceOOM."""

from kubernetes import client
from kubernetes.client.rest import ApiException

from sregym.conductor.oracles.mitigation import MitigationOracle

# Used only when the target had no explicit memory limit originally.
_FALLBACK_FLOOR_BYTES = 64 * 1024 * 1024  # 64Mi


def _parse_mem_to_bytes(value: str) -> int:
    units = {"Ki": 1024, "Mi": 1024**2, "Gi": 1024**3, "k": 1000, "M": 1000**2, "G": 1000**3}
    for suffix, factor in units.items():
        if value.endswith(suffix):
            return int(float(value[: -len(suffix)]) * factor)
    return int(value)


class NightlyRebalanceOOMMitigationOracle(MitigationOracle):
    importance = 1.0

    def evaluate(self) -> dict:
        print("== Nightly Rebalance OOM Mitigation Evaluation ==")
        results = super().evaluate()
        if not results.get("success"):
            return results

        service = self.problem.faulty_service
        namespace = self.problem.namespace

        if self._rebalancer_active():
            print(f"❌ {self.problem.actor_name} CronJob still active; squeeze will recur")
            results["success"] = False
            return results

        if not self._memory_limit_sane(service, namespace):
            print(f"❌ Memory limit on deployment/{service} is still below the restored baseline")
            results["success"] = False
            return results

        if not self._target_pods_healthy(service, namespace):
            print(f"❌ Pods for {service} are not all running/ready without recent OOMKills")
            results["success"] = False
            return results

        print("✅ Rebalancer stopped, memory limit restored, target service healthy")
        results["success"] = True
        return results

    def _rebalancer_active(self) -> bool:
        try:
            cj = client.BatchV1Api().read_namespaced_cron_job(self.problem.actor_name, self.problem.actor_namespace)
        except ApiException as e:
            if e.status == 404:
                return False
            raise
        return not bool(cj.spec.suspend)

    def _memory_limit_sane(self, service: str, namespace: str) -> bool:
        original = getattr(self.problem, "_original_memory_limit", None)
        threshold = _parse_mem_to_bytes(original) if original else _FALLBACK_FLOOR_BYTES
        dep = self.problem.kubectl.get_deployment(service, namespace)
        for container in dep.spec.template.spec.containers:
            limits = (container.resources.limits or {}) if container.resources else {}
            mem = limits.get("memory")
            if mem is None:
                continue
            if _parse_mem_to_bytes(mem) < threshold:
                return False
        return True

    def _target_pods_healthy(self, service: str, namespace: str) -> bool:
        pods = self.problem.kubectl.list_pods(namespace).items
        target = [p for p in pods if (p.metadata.labels or {}).get("io.kompose.service") == service]
        if not target:
            return False
        for pod in target:
            if pod.status.phase != "Running":
                return False
            for cs in pod.status.container_statuses or []:
                if not cs.ready:
                    return False
                last = cs.last_state.terminated if cs.last_state else None
                if last and last.reason == "OOMKilled":
                    return False
        return True
